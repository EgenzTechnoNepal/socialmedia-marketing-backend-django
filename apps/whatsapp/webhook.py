import hashlib
import hmac
import json
import logging
import uuid

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.contacts.views import get_or_create_contact
from apps.messaging.views import apply_status_update, handle_incoming_reaction, save_incoming_message
from apps.whatsapp.models import WhatsAppAccount
from services import storage, whatsapp_client

logger = logging.getLogger(__name__)


def webhook_verify(request):
    mode = request.GET.get("hub.mode", "")
    token = request.GET.get("hub.verify_token", "")
    challenge = request.GET.get("hub.challenge", "")
    if mode != "subscribe":
        return JsonResponse({"status": "error", "message": "Verification failed", "error_type": ""}, status=403)
    if settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN and token == settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN:
        return HttpResponse(challenge, content_type="text/plain")
    if token and WhatsAppAccount.objects.filter(webhook_verify_token=token).exists():
        return HttpResponse(challenge, content_type="text/plain")
    return JsonResponse({"status": "error", "message": "Verification failed", "error_type": ""}, status=403)


def _valid_signature(raw: bytes, header: str, app_secret: str) -> bool:
    if not app_secret or not header:
        return not bool(app_secret)
    expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _extract_content(msg: dict):
    msg_type = msg.get("type") or "text"
    button_id = ""
    flow_response = None
    if msg_type == "text":
        return "text", (msg.get("text") or {}).get("body") or "", None, "", None
    if msg_type == "interactive":
        interactive = msg.get("interactive") or {}
        nfm = interactive.get("nfm_reply") or {}
        if nfm:
            raw = nfm.get("response_json") or nfm.get("body") or ""
            try:
                flow_response = json.loads(raw) if isinstance(raw, str) and raw else (raw if isinstance(raw, dict) else {})
            except json.JSONDecodeError:
                flow_response = {}
            title = nfm.get("name") or nfm.get("body") or "[flow reply]"
            return "nfm_reply", title, None, "", flow_response if isinstance(flow_response, dict) else {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        button_id = reply.get("id") or ""
        return "interactive", reply.get("title") or button_id or "", None, button_id, None
    if msg_type == "button":
        btn = msg.get("button") or {}
        return "button", btn.get("text") or "", None, btn.get("payload") or "", None
    if msg_type in {"image", "video", "audio", "document", "sticker"}:
        media = msg.get(msg_type) or {}
        caption = media.get("caption") or ""
        return msg_type, caption or f"[{msg_type}]", media, "", None
    if msg_type == "location":
        loc = msg.get("location") or {}
        return "location", loc.get("name") or f"{loc.get('latitude')},{loc.get('longitude')}", None, "", None
    return msg_type, f"[{msg_type}]", None, "", None


def _download_media(account: WhatsAppAccount, media: dict, msg_type: str):
    media_id = (media or {}).get("id")
    if not media_id:
        return None
    try:
        url = whatsapp_client.get_media_url(account, media_id)
        if not url:
            return None
        data = whatsapp_client.download_url(url, account.access_token)
        mime = media.get("mime_type") or "application/octet-stream"
        filename = media.get("filename") or f"{msg_type}-{media_id}"
        relative = f"{msg_type}s/{uuid.uuid4().hex}_{filename}"
        storage.save_bytes(relative, data)
        return {"media_url": relative, "media_mime_type": mime, "media_filename": filename}
    except Exception:
        logger.exception("Failed to download inbound media %s", media_id)
        return None


@csrf_exempt
def webhook_receive(request):
    if request.method == "GET":
        return webhook_verify(request)
    logger.info("WhatsApp webhook POST bytes=%s", len(request.body or b""))
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON", "error_type": ""}, status=400)

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            field = change.get("field")
            metadata = value.get("metadata") or {}
            phone_id = metadata.get("phone_number_id") or ""
            account = WhatsAppAccount.objects.filter(phone_id=phone_id).first()
            if account is None:
                continue
            account.decrypt_secrets()
            sig = request.headers.get("X-Hub-Signature-256") or ""
            if account.app_secret and not _valid_signature(request.body, sig, account.app_secret):
                logger.warning("Invalid webhook signature for account %s", account.name)
                continue
            if field == "messages":
                contacts = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
                for msg in value.get("messages") or []:
                    phone = msg.get("from") or ""
                    if not phone:
                        continue
                    profile = contacts.get(phone) or ""
                    if msg.get("type") == "reaction":
                        reaction = msg.get("reaction") or {}
                        handle_incoming_reaction(
                            account,
                            phone,
                            reaction.get("message_id") or "",
                            reaction.get("emoji") or "",
                            profile,
                        )
                        continue
                    contact, created = get_or_create_contact(account.organization_id, phone, profile, account.name)
                    msg_type, content, media, button_id, flow_response = _extract_content(msg)
                    saved_media = _download_media(account, media, msg_type) if media else None
                    reply_wamid = (msg.get("context") or {}).get("id") or ""
                    saved = save_incoming_message(
                        account,
                        contact,
                        msg.get("id") or "",
                        msg_type,
                        content,
                        saved_media,
                        reply_wamid,
                    )
                    if account.auto_read_receipt and msg.get("id"):
                        try:
                            whatsapp_client.mark_read(account, msg["id"])
                        except Exception:
                            logger.exception("read receipt failed")
                    if saved is None:
                        continue
                    try:
                        from apps.chatbot.processor import process_incoming

                        process_incoming(
                            account,
                            contact,
                            created=created,
                            message_text=content,
                            message_type=msg_type,
                            button_id=button_id,
                            flow_response=flow_response,
                            saved_message=saved,
                        )
                    except Exception:
                        logger.exception("chatbot processor failed")
                for status in value.get("statuses") or []:
                    err = ""
                    errors = status.get("errors") or []
                    if errors:
                        err = errors[0].get("title") or errors[0].get("message") or ""
                    apply_status_update(status.get("id") or "", status.get("status") or "", err)
    return JsonResponse({"status": "success", "data": {"status": "ok"}})
