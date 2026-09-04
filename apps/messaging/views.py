import json
import logging
import mimetypes
import re
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


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def send_reaction(request, contact_id, message_id):
    contact = _get_contact(request, contact_id)
    oid = org_id(request)
    data = request.data if isinstance(request.data, dict) else {}
    emoji = data.get("emoji") or ""
    try:
        msg = Message.objects.get(id=message_id, contact=contact, organization_id=oid)
    except Message.DoesNotExist:
        return error("Message not found", http_status=404)
    account_name = msg.whatsapp_account or contact.whatsapp_account
    try:
        account = resolve_account(oid, account_name, contact)
    except APIError as exc:
        return error(str(exc), http_status=exc.status_code)
    metadata = dict(msg.metadata or {}) if isinstance(msg.metadata, dict) else {}
    existing = metadata.get("reactions") or []
    if not isinstance(existing, list):
        existing = []
    user_id = str(request.user.id)
    reactions = []
    for item in existing:
        if not isinstance(item, dict):
            continue
        if item.get("from_user") != user_id:
            reactions.append(item)
    if emoji:
        reactions.append({"emoji": emoji, "from_user": user_id})
    metadata["reactions"] = reactions
    msg.metadata = metadata
    msg.save(update_fields=["metadata", "updated_at"])
    if msg.whatsapp_message_id:
        try:
            whatsapp_client.send_reaction(account, contact.phone_number, msg.whatsapp_message_id, emoji)
        except WhatsAppError:
            logger.exception("Failed to send WhatsApp reaction")
    broadcast_org(
        oid,
        "reaction_update",
        {"message_id": str(msg.id), "contact_id": str(contact.id), "reactions": reactions},
    )
    return success({"message_id": str(msg.id), "reactions": reactions})


def _normalize_template_name(name: str) -> str:
    name = (name or "").lower().replace(" ", "_").replace("-", "_")
    return re.sub(r"[^a-z0-9_]", "", name)


def _template_payload(t: Template) -> dict:
    return {
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


def _apply_template_fields(t: Template, data: dict, *, creating=False):
    if creating or "whatsapp_account" in data:
        t.whatsapp_account = (data.get("whatsapp_account") or t.whatsapp_account or "").strip()
    if creating or "name" in data:
        raw_name = data.get("name") or t.name
        t.name = _normalize_template_name(raw_name)
        if creating and not t.display_name:
            t.display_name = data.get("display_name") or raw_name
    if "display_name" in data and data.get("display_name"):
        t.display_name = data["display_name"]
    if creating or "language" in data:
        t.language = data.get("language") or t.language
    if creating or "category" in data:
        t.category = (data.get("category") or t.category or "").upper()
    if "header_type" in data or creating:
        t.header_type = (data.get("header_type") or t.header_type or "").upper()
    for field in ("header_content", "body_content", "footer_content"):
        if creating or field in data:
            setattr(t, field, data.get(field) or getattr(t, field) or "")
    if "buttons" in data or creating:
        t.buttons = data.get("buttons") or t.buttons or []
    if "sample_values" in data or creating:
        t.sample_values = data.get("sample_values") or t.sample_values or []
    if "add_security_recommendation" in data or creating:
        t.add_security_recommendation = bool(data.get("add_security_recommendation"))
    if "code_expiration_minutes" in data or creating:
        t.code_expiration_minutes = int(data.get("code_expiration_minutes") or 0)


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def templates_collection(request):
    oid = org_id(request)
    if request.method == "GET":
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
        items = [_template_payload(t) for t in qs.order_by("name")[offset : offset + limit]]
        return success(list_payload("templates", items, total, page, limit))

    require_perm(request, "templates", "write")
    data = request.data if isinstance(request.data, dict) else {}
    name = (data.get("name") or "").strip()
    language = (data.get("language") or "").strip()
    category = (data.get("category") or "").strip()
    account_name = (data.get("whatsapp_account") or "").strip()
    is_auth = category.upper() == "AUTHENTICATION"
    if not account_name or not name or not language or not category:
        return error("whatsapp_account, name, language, and category are required", http_status=400)
    if not is_auth and not (data.get("body_content") or "").strip():
        return error("body_content is required", http_status=400)
    try:
        resolve_account(oid, account_name)
    except APIError:
        return error("WhatsApp account not found", http_status=400)
    template_name = _normalize_template_name(name)
    if Template.objects.filter(organization_id=oid, whatsapp_account=account_name, name=template_name).exists():
        return error("Template with this name already exists", http_status=409)
    t = Template(organization_id=oid, status="DRAFT", quality_rating="UNKNOWN", created_by=request.user, updated_by=request.user)
    _apply_template_fields(t, data, creating=True)
    t.name = template_name
    t.display_name = data.get("display_name") or name
    t.save()
    return success(_template_payload(t), http_status=201)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def template_detail(request, template_id):
    oid = org_id(request)
    try:
        t = Template.objects.get(id=template_id, organization_id=oid)
    except Template.DoesNotExist:
        return error("Template not found", http_status=404)
    if request.method == "GET":
        require_perm(request, "templates", "read")
        return success(_template_payload(t))
    if request.method == "DELETE":
        require_perm(request, "templates", "delete")
        if t.meta_template_id:
            try:
                account = resolve_account(oid, t.whatsapp_account)
                whatsapp_client.delete_message_template(account, t.name)
            except Exception:
                logger.exception("Failed to delete template from Meta")
        t.deleted_at = dj_tz.now()
        t.save(update_fields=["deleted_at"])
        return success({"message": "Template deleted successfully"})
    require_perm(request, "templates", "write")
    if t.status == "PENDING":
        return error("Template is pending approval and cannot be modified", http_status=400)
    data = request.data if isinstance(request.data, dict) else {}
    _apply_template_fields(t, data)
    t.updated_by = request.user
    t.save()
    return success(_template_payload(t))


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def publish_template(request, template_id):
    oid = org_id(request)
    require_perm(request, "templates", "write")
    try:
        t = Template.objects.get(id=template_id, organization_id=oid)
    except Template.DoesNotExist:
        return error("Template not found", http_status=404)
    if t.meta_template_id and t.status == "PENDING":
        return error("Template is pending approval and cannot be modified", http_status=400)
    if t.header_type in {"IMAGE", "VIDEO", "DOCUMENT"} and not t.header_content:
        return error(
            f"Template has {t.header_type} header but no media file has been uploaded. Please upload a sample {t.header_type.lower()} first.",
            http_status=400,
        )
    try:
        account = resolve_account(oid, t.whatsapp_account)
        meta_id = whatsapp_client.submit_message_template(account, t)
    except APIError as exc:
        return error(str(exc), http_status=exc.status_code)
    except WhatsAppError as exc:
        return error(f"Failed to submit template to Meta: {exc}", http_status=502)
    old_status = t.status
    t.meta_template_id = meta_id
    t.status = "PENDING"
    t.updated_by = request.user
    t.save(update_fields=["meta_template_id", "status", "updated_by", "updated_at"])
    message = "Template submitted to Meta for approval"
    if old_status and t.meta_template_id and old_status != "DRAFT":
        message = "Template updated and pending re-approval"
    return success(
        {
            "message": message,
            "meta_template_id": meta_id,
            "status": t.status,
            "template": _template_payload(t),
        }
    )


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def sync_templates(request):
    oid = org_id(request)
    require_perm(request, "templates", "write")
    data = request.data if isinstance(request.data, dict) else {}
    account_name = (request.query_params.get("account") or data.get("whatsapp_account") or "").strip()
    if not account_name:
        return error("whatsapp_account is required", http_status=400)
    try:
        account = resolve_account(oid, account_name)
        meta_templates = whatsapp_client.list_message_templates(account)
    except APIError as exc:
        return error(str(exc), http_status=exc.status_code)
    except WhatsAppError as exc:
        return error("Failed to fetch templates from Meta", http_status=502, extra={"detail": str(exc)})
    synced = 0
    for raw in meta_templates:
        quality = raw.get("quality_rating") or ""
        score = raw.get("quality_score") or {}
        if isinstance(score, dict) and score.get("score"):
            quality = score["score"]
        header_type = header_content = body = footer = ""
        buttons = []
        for comp in raw.get("components") or []:
            ctype = (comp.get("type") or "").upper()
            if ctype == "HEADER":
                header_type = comp.get("format") or ""
                header_content = comp.get("text") or ""
            elif ctype == "BODY":
                body = comp.get("text") or ""
            elif ctype == "FOOTER":
                footer = comp.get("text") or ""
            elif ctype == "BUTTONS":
                buttons = list(comp.get("buttons") or [])
        existing = Template.all_objects.filter(
            organization_id=oid,
            whatsapp_account=account.name,
            name=raw.get("name") or "",
            language=raw.get("language") or "",
        ).first()
        if existing:
            existing.meta_template_id = raw.get("id") or existing.meta_template_id
            existing.display_name = raw.get("name") or existing.display_name
            existing.category = raw.get("category") or existing.category
            existing.status = raw.get("status") or existing.status
            existing.header_type = header_type
            existing.header_content = header_content
            existing.body_content = body
            existing.footer_content = footer
            existing.buttons = buttons
            existing.deleted_at = None
            if quality:
                existing.quality_rating = quality
            existing.save()
        else:
            Template.objects.create(
                organization_id=oid,
                whatsapp_account=account.name,
                meta_template_id=raw.get("id") or "",
                name=raw.get("name") or "",
                display_name=raw.get("name") or "",
                language=raw.get("language") or "",
                category=raw.get("category") or "",
                status=raw.get("status") or "PENDING",
                quality_rating=quality or "UNKNOWN",
                header_type=header_type,
                header_content=header_content,
                body_content=body,
                footer_content=footer,
                buttons=buttons,
            )
        synced += 1
    return success({"message": f"Synced {synced} templates", "count": synced})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_template_media(request):
    oid = org_id(request)
    require_perm(request, "templates", "write")
    account_name = (request.data.get("account") or request.query_params.get("account") or "").strip()
    if not account_name:
        return error("account is required", http_status=400)
    try:
        account = resolve_account(oid, account_name)
    except APIError as exc:
        return error(str(exc), http_status=exc.status_code)
    upload = request.FILES.get("file")
    if not upload:
        return error("No file provided", http_status=400)
    mime = upload.content_type or mimetypes.guess_type(upload.name or "")[0] or "application/octet-stream"
    try:
        handle = whatsapp_client.upload_session_media(account, upload.read(), mime, upload.name or "file")
    except WhatsAppError as exc:
        return error(str(exc), http_status=400)
    if not handle:
        return error("Failed to upload media", http_status=502)
    return success({"handle": handle, "mime_type": mime, "filename": upload.name or ""})


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


def handle_incoming_reaction(account: WhatsAppAccount, from_phone: str, wamid: str, emoji: str, profile_name: str = ""):
    if not wamid:
        return
    msg = Message.objects.filter(whatsapp_message_id=wamid).first()
    if msg is None:
        idx = wamid.find("FQIA")
        if idx != -1:
            suffix_start = idx + 8
            if suffix_start < len(wamid):
                suffix = wamid[suffix_start:]
                msg = Message.objects.filter(whatsapp_message_id__contains=suffix).first()
    if msg is None:
        logger.warning("Message not found for reaction wamid=%s", wamid)
        return
    from apps.contacts.views import get_or_create_contact

    contact, _ = get_or_create_contact(account.organization_id, from_phone, profile_name, account.name)
    metadata = dict(msg.metadata or {}) if isinstance(msg.metadata, dict) else {}
    existing = metadata.get("reactions") or []
    if not isinstance(existing, list):
        existing = []
    reactions = [item for item in existing if isinstance(item, dict) and item.get("from_phone") != from_phone]
    if emoji:
        reactions.append({"emoji": emoji, "from_phone": from_phone})
    metadata["reactions"] = reactions
    msg.metadata = metadata
    msg.save(update_fields=["metadata", "updated_at"])
    broadcast_org(
        account.organization_id,
        "reaction_update",
        {"message_id": str(msg.id), "contact_id": str(contact.id), "reactions": reactions},
    )


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
