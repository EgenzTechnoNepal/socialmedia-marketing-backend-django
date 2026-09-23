import logging

from django.utils import timezone as dj_tz

from apps.billing.entitlements import (
    METER_MESSAGE_SENT,
    assert_quota_available,
    record_usage,
)
from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.messaging.views import _preview, _touch_contact
from apps.realtime.hub import broadcast_org, message_ws_payload
from apps.whatsapp.models import WhatsAppAccount
from services import whatsapp_client
from services.whatsapp_client import WhatsAppError

logger = logging.getLogger(__name__)


def send_and_save_text(account: WhatsAppAccount, contact: Contact, text: str, *, sent_by=None) -> Message:
    return _persist_and_send(
        account,
        contact,
        message_type="text",
        content=text or "",
        sender=sent_by,
        send=lambda: whatsapp_client.send_text(account, contact.phone_number, text),
    )


def send_and_save_buttons(account: WhatsAppAccount, contact: Contact, body: str, buttons: list) -> Message | None:
    reply_buttons = []
    cta_buttons = []
    for btn in buttons or []:
        if not isinstance(btn, dict):
            continue
        btn_type = btn.get("type") or ""
        if btn_type == "url":
            cta_buttons.append(btn)
        elif btn_type == "phone":
            phone = btn.get("phone_number") or ""
            if phone:
                cta_buttons.append({"title": btn.get("title"), "url": f"tel:{phone}"})
        else:
            reply_buttons.append(btn)

    last = None
    if reply_buttons:
        last = _persist_and_send(
            account,
            contact,
            message_type="interactive",
            content=body or "",
            interactive={"buttons": reply_buttons},
            send=lambda: whatsapp_client.send_buttons(account, contact.phone_number, body, reply_buttons),
        )
    for cta in cta_buttons:
        last = _persist_and_send(
            account,
            contact,
            message_type="interactive",
            content=body or "",
            interactive={"type": "cta_url", "url": cta.get("url"), "title": cta.get("title")},
            send=lambda b=cta: whatsapp_client.send_cta_url(
                account, contact.phone_number, body, b.get("title") or "Open", b.get("url") or ""
            ),
        )
    return last


def send_and_save_flow(account, contact, flow_id, header, body, cta, flow_token, first_screen) -> Message:
    return _persist_and_send(
        account,
        contact,
        message_type="interactive",
        content=body or "",
        interactive={"type": "flow", "flow_id": flow_id},
        send=lambda: whatsapp_client.send_flow(
            account, contact.phone_number, flow_id, header, body, cta, flow_token, first_screen
        ),
    )


def _persist_and_send(
    account: WhatsAppAccount,
    contact: Contact,
    *,
    message_type: str,
    content: str,
    send,
    interactive=None,
    sender=None,
    template_name="",
    template_params=None,
    media_url="",
    media_mime="",
    event="message.outgoing",
) -> Message:
    assert_quota_available(
        account.organization_id,
        METER_MESSAGE_SENT,
    )
    msg = Message.objects.create(
        organization_id=account.organization_id,
        whatsapp_account=account.name,
        contact=contact,
        direction="outgoing",
        message_type=message_type,
        content=content or "",
        status="pending",
        sent_by_user=sender,
        interactive_data=interactive,
        template_name=template_name,
        template_params=template_params,
        media_url=media_url or "",
        media_mime_type=media_mime or "",
        metadata={},
    )
    try:
        wamid = send()
        msg.whatsapp_message_id = wamid
        msg.status = "sent"
        msg.save(update_fields=["whatsapp_message_id", "status", "updated_at"])
    except WhatsAppError as exc:
        msg.status = "failed"
        msg.error_message = str(exc)
        msg.save(update_fields=["status", "error_message", "updated_at"])
        raise
    record_usage(account.organization_id, METER_MESSAGE_SENT, event_id=f"msg:{msg.id}")
    _touch_contact(contact, _preview(content, message_type), account.name)
    broadcast_org(account.organization_id, "new_message", message_ws_payload(msg, contact))
    try:
        from apps.webhooks.dispatch import dispatch_webhook

        dispatch_webhook(
            account.organization_id,
            event,
            {
                "message_id": str(msg.id),
                "contact_id": str(contact.id),
                "contact_phone": contact.phone_number,
                "contact_name": contact.profile_name or "",
                "message_type": message_type,
                "content": content or "",
                "whatsapp_account": account.name,
                "direction": "outgoing",
            },
        )
    except Exception:
        logger.exception("outbound webhook dispatch failed")
    contact.chatbot_last_message_at = dj_tz.now()
    contact.chatbot_reminder_sent = False
    contact.save(update_fields=["chatbot_last_message_at", "chatbot_reminder_sent", "updated_at"])
    return msg
