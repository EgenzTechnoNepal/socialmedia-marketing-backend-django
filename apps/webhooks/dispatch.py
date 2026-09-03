import hashlib
import hmac
import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

from apps.webhooks.models import Webhook

logger = logging.getLogger(__name__)

EVENT_MESSAGE_INCOMING = "message.incoming"
EVENT_MESSAGE_OUTGOING = "message.outgoing"
EVENT_MESSAGE_SENT = "message.sent"
EVENT_CONTACT_CREATED = "contact.created"
EVENT_TRANSFER_CREATED = "transfer.created"
EVENT_TRANSFER_ASSIGNED = "transfer.assigned"
EVENT_TRANSFER_RESUMED = "transfer.resumed"

AVAILABLE_EVENTS = [
    {"value": EVENT_MESSAGE_INCOMING, "label": "Message Incoming", "description": "When a new message is received from a contact"},
    {"value": EVENT_MESSAGE_SENT, "label": "Message Sent", "description": "When an agent sends a message"},
    {"value": EVENT_MESSAGE_OUTGOING, "label": "Message Outgoing", "description": "When a message is sent to a contact (includes echoes)"},
    {"value": EVENT_CONTACT_CREATED, "label": "Contact Created", "description": "When a new contact is created"},
    {"value": EVENT_TRANSFER_CREATED, "label": "Transfer Created", "description": "When a transfer to human agent is requested"},
    {"value": EVENT_TRANSFER_ASSIGNED, "label": "Transfer Assigned", "description": "When a transfer is assigned to an agent"},
    {"value": EVENT_TRANSFER_RESUMED, "label": "Transfer Resumed", "description": "When chatbot is resumed (transfer closed)"},
]


def dispatch_webhook(organization_id, event_type: str, data):
    threading.Thread(target=_deliver, args=(organization_id, event_type, data), daemon=True).start()


def _deliver(organization_id, event_type: str, data):
    hooks = list(Webhook.objects.filter(organization_id=organization_id, is_active=True))
    payload = json.dumps(
        {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "data": data,
        }
    ).encode("utf-8")
    for hook in hooks:
        events = hook.events or []
        if event_type not in events:
            continue
        _send_with_retries(hook, payload)


def send_webhook_once(hook: Webhook, event_type: str, data) -> None:
    payload = json.dumps(
        {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "data": data,
        }
    ).encode("utf-8")
    _post(hook, payload)


def _send_with_retries(hook: Webhook, payload: bytes):
    last_err = None
    for attempt in range(3):
        try:
            _post(hook, payload)
            return
        except Exception as exc:
            last_err = exc
            logger.warning("webhook %s attempt %s failed: %s", hook.id, attempt + 1, exc)
    logger.error("webhook %s failed after retries: %s", hook.id, last_err)


def _post(hook: Webhook, payload: bytes):
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Whatomate-Webhook/1.0",
    }
    extra = hook.headers or {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(value, str):
                headers[key] = value
    if hook.secret:
        digest = hmac.new(hook.secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={digest}"
    req = urllib.request.Request(hook.url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status < 200 or resp.status >= 300:
                raise RuntimeError(f"webhook status {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"webhook status {exc.code}") from exc
