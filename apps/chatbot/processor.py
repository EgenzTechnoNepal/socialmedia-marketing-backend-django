import logging
import re
from datetime import timedelta

from django.utils import timezone as dj_tz

from apps.chatbot.models import ChatbotFlow, ChatbotSession, ChatbotSessionMessage, ChatbotSettings, KeywordRule
from apps.contacts.models import Contact
from apps.messaging.outbound import send_and_save_buttons, send_and_save_text
from apps.webhooks.dispatch import EVENT_CONTACT_CREATED, EVENT_MESSAGE_INCOMING, dispatch_webhook
from apps.whatsapp.models import WhatsAppAccount

logger = logging.getLogger(__name__)


def load_settings(org_id, account_name: str = "") -> ChatbotSettings | None:
    qs = ChatbotSettings.objects.filter(organization_id=org_id).filter(whatsapp_account__in=[account_name or "", ""])
    account_row = qs.exclude(whatsapp_account="").first() if account_name else None
    if account_row:
        return account_row
    return qs.filter(whatsapp_account="").first() or qs.first()


def is_within_business_hours(hours) -> bool:
    now = dj_tz.now()
    current_day = now.weekday() + 1  # Go uses Sunday=0; Python Monday=0. WhatsApp UI often uses 0=Sunday.
    # Go: time.Weekday() Sunday=0. Our Vue config typically uses 0=Sunday matching JS getDay().
    js_day = (now.weekday() + 1) % 7  # Monday=1 ... Sunday=0
    current_time = now.strftime("%H:%M")
    for item in hours or []:
        if not isinstance(item, dict):
            continue
        day = item.get("day")
        try:
            day_num = int(day)
        except (TypeError, ValueError):
            continue
        if day_num != js_day:
            continue
        if not item.get("enabled"):
            return False
        start = item.get("start_time") or ""
        end = item.get("end_time") or ""
        if start <= current_time <= end:
            return True
        return False
    return False


def get_or_create_session(org_id, contact: Contact, account_name: str, phone: str, timeout_mins: int):
    now = dj_tz.now()
    timeout = now - timedelta(minutes=timeout_mins or 30)
    session = ChatbotSession.objects.filter(
        organization_id=org_id,
        contact=contact,
        whatsapp_account=account_name,
        status="active",
        last_activity_at__gt=timeout,
    ).first()
    if session:
        session.last_activity_at = now
        session.save(update_fields=["last_activity_at", "updated_at"])
        return session, False
    session = ChatbotSession.objects.create(
        organization_id=org_id,
        contact=contact,
        whatsapp_account=account_name,
        phone_number=phone,
        status="active",
        session_data={},
        started_at=now,
        last_activity_at=now,
    )
    return session, True


def log_session_message(session, direction, message, step_name):
    ChatbotSessionMessage.objects.create(
        session=session, direction=direction, message=message or "", step_name=step_name or ""
    )


def match_keyword_rules(org_id, account_name, message_text: str):
    rules = list(
        KeywordRule.objects.filter(organization_id=org_id, is_enabled=True)
        .filter(whatsapp_account__in=[account_name or "", ""])
        .order_by("-priority", "-created_at")
    )
    message_lower = (message_text or "").lower()
    for rule in rules:
        for keyword in rule.keywords or []:
            matched = False
            match_type = rule.match_type or "contains"
            if match_type == "exact":
                matched = message_text == keyword if rule.case_sensitive else message_lower == keyword.lower()
            elif match_type == "starts_with":
                matched = message_text.startswith(keyword) if rule.case_sensitive else message_lower.startswith(keyword.lower())
            elif match_type == "regex":
                try:
                    matched = bool(re.search(keyword, message_text or ""))
                except re.error:
                    matched = False
            else:
                matched = (keyword in message_text) if rule.case_sensitive else (keyword.lower() in message_lower)
            if not matched:
                continue
            content = rule.response_content or {}
            body = content.get("body") or ""
            buttons = []
            for btn in content.get("buttons") or []:
                if isinstance(btn, dict):
                    buttons.append(btn)
            if rule.response_type == "transfer" or body:
                return {"body": body, "buttons": buttons, "response_type": rule.response_type}, True
    return None, False


def match_flow_trigger(org_id, message_text: str) -> ChatbotFlow | None:
    message_lower = (message_text or "").lower()
    for flow in ChatbotFlow.objects.filter(organization_id=org_id, is_enabled=True).order_by("-created_at"):
        for keyword in flow.trigger_keywords or []:
            if keyword and keyword.lower() in message_lower:
                return flow
    return None


def exit_flow(session: ChatbotSession):
    session.current_step = ""
    session.step_retries = 0
    session.status = "completed"
    session.completed_at = dj_tz.now()
    session.save()
    Contact.objects.filter(id=session.contact_id).update(
        chatbot_last_message_at=None, chatbot_reminder_sent=False, updated_at=dj_tz.now()
    )


def process_incoming(account: WhatsAppAccount, contact: Contact, *, created: bool, message_text: str, message_type: str, button_id: str = "", flow_response=None, saved_message=None):
    if created:
        dispatch_webhook(
            account.organization_id,
            EVENT_CONTACT_CREATED,
            {
                "contact_id": str(contact.id),
                "contact_phone": contact.phone_number,
                "contact_name": contact.profile_name or "",
                "whatsapp_account": account.name,
            },
        )
    if saved_message:
        dispatch_webhook(
            account.organization_id,
            EVENT_MESSAGE_INCOMING,
            {
                "message_id": str(saved_message.id),
                "contact_id": str(contact.id),
                "contact_phone": contact.phone_number,
                "contact_name": contact.profile_name or "",
                "message_type": message_type,
                "content": message_text or "",
                "whatsapp_account": account.name,
                "direction": "incoming",
            },
        )
    Contact.objects.filter(id=contact.id).update(chatbot_last_message_at=None, chatbot_reminder_sent=False, updated_at=dj_tz.now())

    from apps.chatbot.transfers import create_transfer_from_keyword, create_transfer_to_queue, has_active_transfer

    if has_active_transfer(account.organization_id, contact.id):
        return
    settings = load_settings(account.organization_id, account.name)
    if settings is None or not settings.is_enabled:
        create_transfer_to_queue(account, contact, "chatbot_disabled")
        return
    if settings.business_hours_enabled and settings.business_hours:
        if not is_within_business_hours(settings.business_hours) and not settings.allow_automated_outside_hours:
            if settings.out_of_hours_message:
                send_and_save_text(account, contact, settings.out_of_hours_message)
            return
    if not message_text:
        return

    session, is_new = get_or_create_session(
        account.organization_id, contact, account.name, contact.phone_number, settings.session_timeout_mins or 30
    )
    log_session_message(session, "incoming", message_text, "keyword_check")

    keyword, matched = match_keyword_rules(account.organization_id, account.name, message_text)
    if matched and keyword["response_type"] == "transfer":
        if settings.business_hours_enabled and settings.business_hours and not is_within_business_hours(settings.business_hours):
            if settings.out_of_hours_message:
                send_and_save_text(account, contact, settings.out_of_hours_message)
            return
        if keyword["body"]:
            send_and_save_text(account, contact, keyword["body"])
        create_transfer_from_keyword(account, contact)
        return

    if session.current_flow_id:
        flow = ChatbotFlow.objects.filter(id=session.current_flow_id, organization_id=account.organization_id).first()
        if not flow or not flow.graph:
            exit_flow(session)
            return
        from apps.chatbot.graph import run_chat_graph

        try:
            run_chat_graph(account, contact, session, flow, message_text, button_id, flow_response)
        except Exception:
            logger.exception("chat graph runner failed")
        return

    flow = match_flow_trigger(account.organization_id, message_text)
    if flow:
        if not flow.graph:
            return
        session.current_flow = flow
        session.current_step = ""
        session.step_retries = 0
        session.session_data = {"_flow_id": str(flow.id), "_flow_name": flow.name}
        from apps.chatbot.graph import run_chat_graph

        try:
            run_chat_graph(account, contact, session, flow, message_text, button_id, flow_response)
        except Exception:
            logger.exception("chat graph runner failed at flow start")
        return

    if is_new and settings.default_response:
        buttons = [b for b in (settings.greeting_buttons or []) if isinstance(b, dict)]
        if buttons:
            send_and_save_buttons(account, contact, settings.default_response, buttons)
        else:
            send_and_save_text(account, contact, settings.default_response)
        log_session_message(session, "outgoing", settings.default_response, "greeting")
        return

    if matched and keyword["response_type"] != "transfer":
        if keyword["buttons"]:
            send_and_save_buttons(account, contact, keyword["body"], keyword["buttons"])
        else:
            send_and_save_text(account, contact, keyword["body"])
        log_session_message(session, "outgoing", keyword["body"], "keyword_response")
        return

    if settings.ai_enabled and settings.ai_provider and settings.ai_api_key:
        try:
            from apps.chatbot.ai import generate_ai_response

            ai_text = generate_ai_response(settings, session, message_text)
        except Exception:
            logger.exception("AI response failed")
            ai_text = ""
        if ai_text:
            send_and_save_text(account, contact, ai_text)
            log_session_message(session, "outgoing", ai_text, "ai_response")
            return

    if settings.fallback_message and not is_new:
        buttons = [b for b in (settings.fallback_buttons or []) if isinstance(b, dict)]
        if buttons:
            send_and_save_buttons(account, contact, settings.fallback_message, buttons)
        else:
            send_and_save_text(account, contact, settings.fallback_message)
        log_session_message(session, "outgoing", settings.fallback_message, "fallback_response")
