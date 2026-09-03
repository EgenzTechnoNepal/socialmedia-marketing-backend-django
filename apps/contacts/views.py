from django.db import connection
from django.db.models import Count, Q
from django.utils import timezone as dj_tz
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import User
from apps.common.envelope import error, success
from apps.common.http import (
    iso,
    list_payload,
    normalize_phone,
    org_id,
    parse_pagination,
    require_perm,
    service_window_open,
)
from apps.common.permissions import CookieAuthenticated
from apps.contacts.models import Contact, ConversationNote
from apps.messaging.models import Message
from apps.realtime.hub import broadcast_org

VALID_TAG_COLORS = {"blue", "red", "green", "yellow", "purple", "gray", ""}


def _can_read_all_contacts(request) -> bool:
    from apps.common.http import is_super

    return is_super(request) or request.user.has_permission("contacts", "read")


def _scoped_contacts(request, qs):
    if _can_read_all_contacts(request):
        return qs
    return qs.filter(assigned_user_id=request.user.id)


def _unread_count(contact_id) -> int:
    return Message.objects.filter(
        contact_id=contact_id, direction="incoming"
    ).exclude(status="read").count()


def contact_payload(contact) -> dict:
    tags = contact.tags if isinstance(contact.tags, list) else []
    assigned = str(contact.assigned_user_id) if contact.assigned_user_id else None
    name = contact.profile_name or ""
    return {
        "id": str(contact.id),
        "phone_number": contact.phone_number,
        "name": name,
        "profile_name": name,
        "avatar_url": "",
        "status": "active",
        "tags": tags,
        "metadata": contact.metadata or {},
        "last_message_at": iso(contact.last_message_at),
        "last_message_preview": contact.last_message_preview or "",
        "unread_count": _unread_count(contact.id),
        "assigned_user_id": assigned,
        "whatsapp_account": contact.whatsapp_account or "",
        "last_inbound_at": iso(contact.last_inbound_at),
        "service_window_open": service_window_open(contact.last_inbound_at),
        "marketing_opt_out": bool(contact.marketing_opt_out),
        "created_at": iso(contact.created_at),
        "updated_at": iso(contact.updated_at),
    }


def get_or_create_contact(organization_id, phone: str, profile_name: str = "", account_name: str = "") -> tuple[Contact, bool]:
    phone = normalize_phone(phone)
    contact = Contact.objects.filter(organization_id=organization_id, phone_number=phone).first()
    if contact:
        updates = {}
        if profile_name and not contact.profile_name:
            updates["profile_name"] = profile_name
        if account_name and not contact.whatsapp_account:
            updates["whatsapp_account"] = account_name
        if updates:
            for k, v in updates.items():
                setattr(contact, k, v)
            contact.save(update_fields=[*updates.keys(), "updated_at"])
        return contact, False
    return (
        Contact.objects.create(
            organization_id=organization_id,
            phone_number=phone,
            profile_name=profile_name or phone,
            whatsapp_account=account_name,
            tags=[],
            metadata={},
        ),
        True,
    )


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def contacts_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        page, limit, offset = parse_pagination(request)
        qs = _scoped_contacts(request, Contact.objects.filter(organization_id=oid))
        search = (request.query_params.get("search") or "").strip()[:1000]
        if search:
            qs = qs.filter(Q(phone_number__icontains=search) | Q(profile_name__icontains=search))
        tags_param = request.query_params.get("tags") or ""
        if tags_param:
            for tag in [t.strip() for t in tags_param.split(",") if t.strip()]:
                qs = qs.filter(tags__contains=[tag])
        total = qs.count()
        contacts = list(qs.order_by("-last_message_at", "-created_at")[offset : offset + limit])
        return success(list_payload("contacts", [contact_payload(c) for c in contacts], total, page, limit))

    require_perm(request, "contacts", "write")
    data = request.data if isinstance(request.data, dict) else {}
    phone = normalize_phone(data.get("phone_number") or "")
    if not phone:
        return error("phone_number is required", http_status=400)
    if Contact.objects.filter(organization_id=oid, phone_number=phone).exists():
        return error("Contact already exists", http_status=409)
    contact = Contact.objects.create(
        organization_id=oid,
        phone_number=phone,
        profile_name=data.get("profile_name") or phone,
        whatsapp_account=data.get("whatsapp_account") or "",
        tags=data.get("tags") or [],
        metadata=data.get("metadata") or {},
    )
    return success(contact_payload(contact), http_status=201)


def _get_contact(request, contact_id):
    oid = org_id(request)
    qs = _scoped_contacts(request, Contact.objects.filter(id=contact_id, organization_id=oid))
    contact = qs.first()
    if not contact:
        from apps.common.exceptions import APIError

        raise APIError("Contact not found", status_code=404)
    return contact


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def contact_detail(request, contact_id):
    contact = _get_contact(request, contact_id)
    if request.method == "GET":
        return success(contact_payload(contact))
    if request.method == "DELETE":
        require_perm(request, "contacts", "delete")
        contact.deleted_at = dj_tz.now()
        contact.save(update_fields=["deleted_at"])
        return success({"message": "Contact deleted"})
    require_perm(request, "contacts", "write")
    data = request.data if isinstance(request.data, dict) else {}
    if "profile_name" in data:
        contact.profile_name = data.get("profile_name") or contact.profile_name
    if "whatsapp_account" in data:
        contact.whatsapp_account = data.get("whatsapp_account") or ""
    if "tags" in data:
        contact.tags = data.get("tags") or []
    if "metadata" in data:
        contact.metadata = data.get("metadata") or {}
    if data.get("clear_assigned_agent"):
        contact.assigned_user_id = None
    elif "assigned_user_id" in data:
        contact.assigned_user_id = data.get("assigned_user_id") or None
    contact.save()
    return success(contact_payload(contact))


@api_view(["PUT"])
@permission_classes([CookieAuthenticated])
def assign_contact(request, contact_id):
    require_perm(request, "contacts", "write")
    contact = _get_contact(request, contact_id)
    data = request.data if isinstance(request.data, dict) else {}
    user_id = data.get("user_id")
    contact.assigned_user_id = user_id or None
    contact.save(update_fields=["assigned_user_id", "updated_at"])
    return success({"message": "Contact assigned", "assigned_user_id": str(contact.assigned_user_id) if contact.assigned_user_id else None})


@api_view(["PUT"])
@permission_classes([CookieAuthenticated])
def update_contact_tags(request, contact_id):
    require_perm(request, "contacts", "write")
    contact = _get_contact(request, contact_id)
    data = request.data if isinstance(request.data, dict) else {}
    contact.tags = data.get("tags") or []
    contact.save(update_fields=["tags", "updated_at"])
    return success({"message": "Tags updated", "tags": contact.tags})


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def contact_session_data(request, contact_id):
    _get_contact(request, contact_id)
    return success(
        {
            "session_id": None,
            "flow_id": None,
            "flow_name": "",
            "session_data": {},
            "panel_config": {"sections": []},
        }
    )


def _note_payload(note) -> dict:
    name = ""
    try:
        name = note.created_by.full_name or note.created_by.email
    except Exception:
        pass
    return {
        "id": str(note.id),
        "contact_id": str(note.contact_id),
        "created_by_id": str(note.created_by_id),
        "created_by_name": name,
        "content": note.content,
        "created_at": iso(note.created_at),
        "updated_at": iso(note.updated_at),
    }


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def notes_collection(request, contact_id):
    contact = _get_contact(request, contact_id)
    oid = org_id(request)
    if request.method == "GET":
        require_perm(request, "chat", "read")
        qs = ConversationNote.objects.filter(contact=contact, organization_id=oid).order_by("-created_at")
        notes = list(qs[:50])
        return success({"notes": [_note_payload(n) for n in notes], "total": qs.count(), "has_more": qs.count() > 50})
    require_perm(request, "chat", "write")
    data = request.data if isinstance(request.data, dict) else {}
    content = (data.get("content") or "").strip()
    if not content:
        return error("content is required", http_status=400)
    note = ConversationNote.objects.create(
        organization_id=oid, contact=contact, created_by=request.user, content=content
    )
    payload = _note_payload(note)
    broadcast_org(oid, "conversation_note_created", payload)
    return success(payload, http_status=201)


@api_view(["PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def note_detail(request, contact_id, note_id):
    contact = _get_contact(request, contact_id)
    require_perm(request, "chat", "write")
    try:
        note = ConversationNote.objects.get(id=note_id, contact=contact, created_by=request.user)
    except ConversationNote.DoesNotExist:
        return error("Note not found", http_status=404)
    oid = org_id(request)
    if request.method == "DELETE":
        note.deleted_at = dj_tz.now()
        note.save(update_fields=["deleted_at"])
        broadcast_org(oid, "conversation_note_deleted", {"id": str(note.id), "contact_id": str(contact.id)})
        return success({"message": "Note deleted"})
    data = request.data if isinstance(request.data, dict) else {}
    if data.get("content"):
        note.content = data["content"]
        note.save(update_fields=["content", "updated_at"])
    payload = _note_payload(note)
    broadcast_org(oid, "conversation_note_updated", payload)
    return success(payload)


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def tags_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        require_perm(request, "tags", "read")
        page, limit, offset = parse_pagination(request)
        search = (request.query_params.get("search") or "").lower()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name, color, created_at, updated_at FROM tags WHERE organization_id = %s ORDER BY name",
                [str(oid)],
            )
            rows = cursor.fetchall()
        items = []
        for name, color, created_at, updated_at in rows:
            if search and search not in (name or "").lower() and search not in (color or "").lower():
                continue
            items.append(
                {
                    "name": name,
                    "color": color or "",
                    "created_at": iso(created_at),
                    "updated_at": iso(updated_at),
                }
            )
        total = len(items)
        sliced = items[offset : offset + limit]
        return success(list_payload("tags", sliced, total, page, limit))

    require_perm(request, "tags", "write")
    data = request.data if isinstance(request.data, dict) else {}
    name = (data.get("name") or "").strip()
    color = data.get("color") or "gray"
    if not name:
        return error("name is required", http_status=400)
    if color not in VALID_TAG_COLORS:
        return error("Invalid tag color", http_status=400)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO tags (organization_id, name, color, created_at, updated_at)
            VALUES (%s, %s, %s, NOW(), NOW())
            ON CONFLICT (organization_id, name) DO NOTHING
            """,
            [str(oid), name, color],
        )
    return success({"name": name, "color": color}, http_status=201)


@api_view(["PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def tag_detail(request, name):
    oid = org_id(request)
    require_perm(request, "tags", "write" if request.method == "PUT" else "delete")
    if request.method == "DELETE":
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM tags WHERE organization_id = %s AND name = %s", [str(oid), name])
        return success({"message": "Tag deleted"})
    data = request.data if isinstance(request.data, dict) else {}
    color = data.get("color") or "gray"
    new_name = (data.get("name") or name).strip()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE tags SET name = %s, color = %s, updated_at = NOW() WHERE organization_id = %s AND name = %s",
            [new_name, color, str(oid), name],
        )
    return success({"name": new_name, "color": color})
