import json
import logging
import mimetypes
import uuid
from datetime import timedelta

from django.http import FileResponse, Http404
from django.utils import timezone as dj_tz
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser

from apps.billing.entitlements import METER_MESSAGE_SENT, assert_outbound_allowed, record_usage
from apps.common.envelope import error, success
from apps.common.exceptions import APIError
from apps.common.http import iso, list_payload, org_id, parse_pagination, require_perm
from apps.common.permissions import CookieAuthenticated
from apps.contacts.models import Contact
from apps.contacts.views import _get_contact, contact_payload
from apps.messaging.models import CannedResponse, Message, Template
from apps.realtime.hub import broadcast_org, message_ws_payload
from apps.whatsapp.models import WhatsAppAccount
from services import storage, whatsapp_client
from services.whatsapp_client import WhatsAppError

logger = logging.getLogger(__name__)

STATUS_RANK = {"pending": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4, "received": 1}


def message_payload(msg: Message) -> dict:
    reply_id = str(msg.reply_to_message_id) if msg.reply_to_message_id else None
    reply_preview = None
    reply = getattr(msg, "reply_to_message", None)
    if msg.is_reply and reply:
        reply_preview = {
            "id": str(reply.id),
            "content": {"body": reply.content or ""},
            "message_type": reply.message_type,
            "direction": reply.direction,
        }
    reactions = []
    if isinstance(msg.metadata, dict):
        reactions = msg.metadata.get("reactions") or []
    return {
        "id": str(msg.id),
        "contact_id": str(msg.contact_id),
        "direction": msg.direction,
        "message_type": msg.message_type,
        "content": {"body": msg.content or ""},
        "media_url": msg.media_url or "",
        "media_mime_type": msg.media_mime_type or "",
        "media_filename": msg.media_filename or "",
        "interactive_data": msg.interactive_data,
        "status": msg.status,
        "wamid": msg.whatsapp_message_id or "",
        "error_message": msg.error_message or "",
        "is_reply": bool(msg.is_reply),
        "reply_to_message_id": reply_id,
        "reply_to_message": reply_preview,
        "reactions": reactions,
        "whatsapp_account": msg.whatsapp_account or "",
        "created_at": iso(msg.created_at),
        "updated_at": iso(msg.updated_at),
    }


def resolve_account(oid, name: str = "", contact: Contact | None = None) -> WhatsAppAccount:
    qs = WhatsAppAccount.objects.filter(organization_id=oid, status="active")
    account = None
    if name:
        account = qs.filter(name=name).first()
    if account is None and contact and contact.whatsapp_account:
        account = qs.filter(name=contact.whatsapp_account).first()
    if account is None:
        account = qs.filter(is_default_outgoing=True).first() or qs.first()
    if account is None:
        raise APIError("No WhatsApp account configured", status_code=400)
    return account.decrypt_secrets()


def _preview(text: str, msg_type: str) -> str:
    if msg_type not in {"text", "button_reply", "nfm_reply"}:
        return f"[{msg_type}]"
    text = text or ""
    return text[:97] + "..." if len(text) > 100 else text


def _touch_contact(contact: Contact, preview: str, account_name: str, inbound=False):
    now = dj_tz.now()
    contact.last_message_at = now
    contact.last_message_preview = preview
    contact.whatsapp_account = account_name or contact.whatsapp_account
    if inbound:
        contact.last_inbound_at = now
        contact.is_read = False
    contact.save(
        update_fields=["last_message_at", "last_message_preview", "whatsapp_account", "last_inbound_at", "is_read", "updated_at"]
        if inbound
        else ["last_message_at", "last_message_preview", "whatsapp_account", "updated_at"]
    )


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def contact_messages(request, contact_id):
    contact = _get_contact(request, contact_id)
    oid = org_id(request)
    if request.method == "GET":
        qs = Message.objects.filter(contact=contact, organization_id=oid)
        account_filter = request.query_params.get("account")
        if account_filter:
            qs = qs.filter(whatsapp_account=account_filter)
        total = qs.count()
        before_id = request.query_params.get("before_id")
        try:
            limit = int(request.query_params.get("limit") or 50)
        except ValueError:
            limit = 50
        if limit < 1 or limit > 100:
            limit = 50
        if before_id:
            try:
                before = Message.objects.get(id=before_id)
                qs = qs.filter(created_at__lt=before.created_at)
            except Message.DoesNotExist:
                pass
            messages = list(qs.select_related("reply_to_message").order_by("-created_at")[:limit])
            messages.reverse()
            _mark_read(oid, contact)
            return success(
                {
                    "messages": [message_payload(m) for m in messages],
                    "total": total,
                    "limit": limit,
                    "has_more": len(messages) == limit,
                }
            )
        page, limit, _offset = parse_pagination(request, default_limit=limit, max_limit=100)
        offset = total - (page * limit)
        query_limit = limit
        if offset < 0:
            query_limit = limit + offset
            offset = 0
        messages = list(
            qs.select_related("reply_to_message").order_by("created_at")[offset : offset + max(query_limit, 0)]
        )
        _mark_read(oid, contact)
        return success(
            {
                "messages": [message_payload(m) for m in messages],
                "total": total,
                "page": page,
                "limit": limit,
                "has_more": offset > 0,
            }
        )

    require_perm(request, "chat", "write")
    data = request.data if isinstance(request.data, dict) else {}
    return _send_text_or_interactive(request, contact, data)


def _mark_read(oid, contact: Contact):
    Message.objects.filter(
        organization_id=oid, contact=contact, direction="incoming"
    ).exclude(status="read").update(status="read", updated_at=dj_tz.now())
    contact.is_read = True
    contact.save(update_fields=["is_read", "updated_at"])


def _send_text_or_interactive(request, contact: Contact, data: dict):
    oid = org_id(request)
    assert_outbound_allowed(oid)
    account = resolve_account(oid, data.get("whatsapp_account") or "", contact)
    msg_type = data.get("type") or "text"
    content_obj = data.get("content") or {}
    body = content_obj.get("body") if isinstance(content_obj, dict) else ""
    if not body and data.get("interactive"):
        body = (data["interactive"] or {}).get("body") or ""
    if not body and msg_type == "text":
        return error("content.body is required", http_status=400)

    reply = None
    reply_id = data.get("reply_to_message_id")
    if reply_id:
        reply = Message.objects.filter(id=reply_id, organization_id=oid).first()

    msg = Message.objects.create(
        organization_id=oid,
        whatsapp_account=account.name,
        contact=contact,
        direction="outgoing",
        message_type="interactive" if msg_type == "interactive" else "text",
        content=body or "",
        status="pending",
        is_reply=bool(reply),
        reply_to_message=reply,
        sent_by_user=request.user,
        interactive_data=data.get("interactive") if msg_type == "interactive" else None,
        metadata={},
    )
    try:
        if msg_type == "interactive":
            interactive = data.get("interactive") or {}
            buttons = interactive.get("buttons") or []
            wamid = whatsapp_client.send_buttons(account, contact.phone_number, body, buttons)
        else:
            wamid = whatsapp_client.send_text(
                account,
                contact.phone_number,
                body,
                reply.whatsapp_message_id if reply else "",
            )
        msg.whatsapp_message_id = wamid
        msg.status = "sent"
        msg.save(update_fields=["whatsapp_message_id", "status", "updated_at"])
    except WhatsAppError as exc:
        msg.status = "failed"
        msg.error_message = str(exc)
        msg.save(update_fields=["status", "error_message", "updated_at"])
    record_usage(oid, METER_MESSAGE_SENT, event_id=f"msg:{msg.id}")
    _touch_contact(contact, _preview(body, msg.message_type), account.name)
    broadcast_org(oid, "new_message", message_ws_payload(msg, contact))
    try:
        from apps.webhooks.dispatch import EVENT_MESSAGE_SENT, dispatch_webhook

        dispatch_webhook(
            oid,
            EVENT_MESSAGE_SENT,
            {
                "message_id": str(msg.id),
                "contact_id": str(contact.id),
                "contact_phone": contact.phone_number,
                "contact_name": contact.profile_name or "",
                "message_type": msg.message_type,
                "content": body or "",
                "whatsapp_account": account.name,
                "direction": "outgoing",
                "sent_by_user_id": str(request.user.id),
            },
        )
    except Exception:
        logger.exception("message.sent webhook failed")
    return success(message_payload(msg), http_status=201)


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def mark_contact_read(request, contact_id):
    contact = _get_contact(request, contact_id)
    _mark_read(org_id(request), contact)
    return success({"status": "ok"})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def send_media(request):
    oid = org_id(request)
    require_perm(request, "chat", "write")
    assert_outbound_allowed(oid)
    contact_id = request.data.get("contact_id")
    contact = _get_contact(request, contact_id)
    upload = request.FILES.get("file")
    if not upload:
        return error("file is required", http_status=400)
    msg_type = request.data.get("type") or "image"
    caption = request.data.get("caption") or ""
    account = resolve_account(oid, request.data.get("whatsapp_account") or "", contact)
    data = upload.read()
    filename = upload.name or f"{msg_type}.bin"
    mime = upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    relative = f"{msg_type}s/{uuid.uuid4().hex}_{filename}"
    storage.save_bytes(relative, data)
    msg = Message.objects.create(
        organization_id=oid,
        whatsapp_account=account.name,
        contact=contact,
        direction="outgoing",
        message_type=msg_type,
        content=caption,
        media_url=relative,
        media_mime_type=mime,
        media_filename=filename,
        status="pending",
        sent_by_user=request.user,
        metadata={},
    )
    try:
        media_id = whatsapp_client.upload_media(account, data, mime, filename)
        wamid = whatsapp_client.send_media(account, contact.phone_number, msg_type, media_id, caption, filename)
        msg.whatsapp_message_id = wamid
        msg.status = "sent"
        msg.save(update_fields=["whatsapp_message_id", "status", "updated_at"])
    except WhatsAppError as exc:
        msg.status = "failed"
        msg.error_message = str(exc)
        msg.save(update_fields=["status", "error_message", "updated_at"])
    record_usage(oid, METER_MESSAGE_SENT, event_id=f"msg:{msg.id}")
    _touch_contact(contact, _preview(caption, msg_type), account.name)
    broadcast_org(oid, "new_message", message_ws_payload(msg, contact))
    return success(message_payload(msg), http_status=201)


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def send_template(request):
    oid = org_id(request)
    require_perm(request, "chat", "write")
    assert_outbound_allowed(oid)
    data = request.data if isinstance(request.data, dict) else {}
    contact = None
    if data.get("contact_id"):
        contact = _get_contact(request, data["contact_id"])
    elif data.get("phone_number"):
        from apps.contacts.views import get_or_create_contact

        contact, _ = get_or_create_contact(oid, data["phone_number"])
    else:
        return error("contact_id or phone_number is required", http_status=400)
    account = resolve_account(oid, data.get("account_name") or data.get("whatsapp_account") or "", contact)
    template = None
    if data.get("template_id"):
        template = Template.objects.filter(id=data["template_id"], organization_id=oid).first()
    elif data.get("template_name"):
        template = Template.objects.filter(
            organization_id=oid, name=data["template_name"], whatsapp_account=account.name
        ).first()
    if not template:
        return error("Template not found", http_status=404)
    if template.status != "APPROVED":
        return error("Template is not approved", http_status=400)
    params = data.get("template_params") or {}
    components = []
    if isinstance(params, dict) and params:
        body_params = [{"type": "text", "text": str(v)} for v in params.values()]
        components.append({"type": "body", "parameters": body_params})
    elif isinstance(params, list) and params:
        body_params = [{"type": "text", "text": str(v)} for v in params]
        components.append({"type": "body", "parameters": body_params})
    msg = Message.objects.create(
        organization_id=oid,
        whatsapp_account=account.name,
        contact=contact,
        direction="outgoing",
        message_type="template",
        content=template.body_content or template.name,
        template_name=template.name,
        template_params=params if isinstance(params, dict) else {"values": params},
        status="pending",
        sent_by_user=request.user,
        metadata={},
    )
    try:
        wamid = whatsapp_client.send_template(
            account, contact.phone_number, template.name, template.language, components
        )
        msg.whatsapp_message_id = wamid
        msg.status = "sent"
        msg.save(update_fields=["whatsapp_message_id", "status", "updated_at"])
    except WhatsAppError as exc:
        msg.status = "failed"
        msg.error_message = str(exc)
        msg.save(update_fields=["status", "error_message", "updated_at"])
    record_usage(oid, METER_MESSAGE_SENT, event_id=f"msg:{msg.id}")
    _touch_contact(contact, f"[template] {template.name}", account.name)
    broadcast_org(oid, "new_message", message_ws_payload(msg, contact))
    return success(message_payload(msg), http_status=201)


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def serve_media(request, message_id):
    oid = org_id(request)
    try:
        msg = Message.objects.get(id=message_id, organization_id=oid)
    except Message.DoesNotExist:
        return error("Message not found", http_status=404)
    _get_contact(request, msg.contact_id)
    if not msg.media_url:
        return error("No media", http_status=404)
    path = storage.absolute_path(msg.media_url)
    if not path.exists():
        raise Http404("Media missing")
    return FileResponse(path.open("rb"), filename=msg.media_filename or path.name)


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def templates_collection(request):
    oid = org_id(request)
    require_perm(request, "templates", "read")
    page, limit, offset = parse_pagination(request)
    qs = Template.objects.filter(organization_id=oid)
    if request.query_params.get("account"):
        qs = qs.filter(whatsapp_account=request.query_params["account"])
    if request.query_params.get("status"):
        qs = qs.filter(status=request.query_params["status"])
    if request.query_params.get("category"):
        qs = qs.filter(category=request.query_params["category"])
    search = (request.query_params.get("search") or "").strip()
    if search:
        qs = qs.filter(name__icontains=search)
    total = qs.count()
    items = []
    for t in qs.order_by("name")[offset : offset + limit]:
        items.append(
            {
                "id": str(t.id),
                "whatsapp_account": t.whatsapp_account,
                "meta_template_id": t.meta_template_id or "",
                "name": t.name,
                "display_name": t.display_name or t.name,
                "language": t.language,
                "category": t.category or "",
                "status": t.status,
                "header_type": t.header_type or "",
                "header_content": t.header_content or "",
                "body_content": t.body_content or "",
                "footer_content": t.footer_content or "",
                "buttons": t.buttons or [],
                "sample_values": t.sample_values or [],
                "add_security_recommendation": t.add_security_recommendation,
                "code_expiration_minutes": t.code_expiration_minutes,
                "quality_rating": t.quality_rating or "",
                "created_at": iso(t.created_at),
                "updated_at": iso(t.updated_at),
            }
        )
    return success(list_payload("templates", items, total, page, limit))


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def template_detail(request, template_id):
    oid = org_id(request)
    require_perm(request, "templates", "read")
    try:
        t = Template.objects.get(id=template_id, organization_id=oid)
    except Template.DoesNotExist:
        return error("Template not found", http_status=404)
    return success(
        {
            "id": str(t.id),
            "whatsapp_account": t.whatsapp_account,
            "name": t.name,
            "display_name": t.display_name or t.name,
            "language": t.language,
            "category": t.category or "",
            "status": t.status,
            "header_type": t.header_type or "",
            "header_content": t.header_content or "",
            "body_content": t.body_content or "",
            "footer_content": t.footer_content or "",
            "buttons": t.buttons or [],
            "sample_values": t.sample_values or [],
            "created_at": iso(t.created_at),
            "updated_at": iso(t.updated_at),
        }
    )


def _canned_payload(row: CannedResponse) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "shortcut": row.shortcut or "",
        "content": row.content,
        "category": row.category or "",
        "is_active": row.is_active,
        "usage_count": row.usage_count,
        "buttons": row.buttons or [],
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def canned_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        page, limit, offset = parse_pagination(request)
        qs = CannedResponse.objects.filter(organization_id=oid)
        if request.query_params.get("category"):
            qs = qs.filter(category=request.query_params["category"])
        if request.query_params.get("active_only") == "true":
            qs = qs.filter(is_active=True)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        total = qs.count()
        items = [_canned_payload(r) for r in qs.order_by("name")[offset : offset + limit]]
        return success(list_payload("canned_responses", items, total, page, limit))
    data = request.data if isinstance(request.data, dict) else {}
    name = (data.get("name") or "").strip()
    content = data.get("content") or ""
    if not name or not content:
        return error("name and content are required", http_status=400)
    row = CannedResponse.objects.create(
        organization_id=oid,
        name=name,
        shortcut=data.get("shortcut") or "",
        content=content,
        category=data.get("category") or "",
        is_active=data.get("is_active", True),
        buttons=data.get("buttons") or [],
        created_by=request.user,
    )
    return success(_canned_payload(row), http_status=201)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def canned_detail(request, canned_id):
    oid = org_id(request)
    try:
        row = CannedResponse.objects.get(id=canned_id, organization_id=oid)
    except CannedResponse.DoesNotExist:
        return error("Canned response not found", http_status=404)
    if request.method == "GET":
        return success(_canned_payload(row))
    if request.method == "DELETE":
        row.deleted_at = dj_tz.now()
        row.save(update_fields=["deleted_at"])
        return success({"message": "Deleted"})
    data = request.data if isinstance(request.data, dict) else {}
    for field in ("name", "shortcut", "content", "category"):
        if field in data:
            setattr(row, field, data.get(field) or "")
    if "is_active" in data:
        row.is_active = bool(data["is_active"])
    if "buttons" in data:
        row.buttons = data.get("buttons") or []
    row.save()
    return success(_canned_payload(row))


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def canned_use(request, canned_id):
    oid = org_id(request)
    try:
        row = CannedResponse.objects.get(id=canned_id, organization_id=oid)
    except CannedResponse.DoesNotExist:
        return error("Canned response not found", http_status=404)
    row.usage_count = (row.usage_count or 0) + 1
    row.save(update_fields=["usage_count", "updated_at"])
    return success(_canned_payload(row))


def save_incoming_message(account: WhatsAppAccount, contact: Contact, wamid: str, msg_type: str, content: str, media=None, reply_wamid: str = ""):
    if wamid and Message.objects.filter(whatsapp_message_id=wamid).exists():
        return None
    reply = None
    if reply_wamid:
        reply = Message.objects.filter(whatsapp_message_id=reply_wamid).first()
    msg = Message.objects.create(
        organization_id=account.organization_id,
        whatsapp_account=account.name,
        contact=contact,
        whatsapp_message_id=wamid,
        direction="incoming",
        message_type=msg_type or "text",
        content=content or "",
        media_url=(media or {}).get("media_url") or "",
        media_mime_type=(media or {}).get("media_mime_type") or "",
        media_filename=(media or {}).get("media_filename") or "",
        status="received",
        is_reply=bool(reply),
        reply_to_message=reply,
        metadata={},
    )
    _touch_contact(contact, _preview(content, msg_type or "text"), account.name, inbound=True)
    broadcast_org(account.organization_id, "new_message", message_ws_payload(msg, contact))
    return msg


def apply_status_update(wamid: str, status: str, error_message: str = ""):
    msg = Message.objects.filter(whatsapp_message_id=wamid).first()
    if not msg:
        return
    current = STATUS_RANK.get(msg.status, -1)
    incoming = STATUS_RANK.get(status, -1)
    if incoming < current and status != "failed":
        return
    msg.status = status
    if error_message:
        msg.error_message = error_message
    msg.save(update_fields=["status", "error_message", "updated_at"])
    payload = {"message_id": str(msg.id), "status": status}
    if error_message:
        payload["error_message"] = error_message
    broadcast_org(msg.organization_id, "status_update", payload)
