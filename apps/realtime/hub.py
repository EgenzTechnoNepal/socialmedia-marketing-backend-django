import json
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import timezone as dj_tz

from apps.common.tokens import redis_client

logger = logging.getLogger(__name__)


def org_group(org_id) -> str:
    return f"org_{org_id}"


def online_key(org_id) -> str:
    return f"ws:online:{org_id}"


def mark_online(org_id, user_id):
    try:
        redis_client().sadd(online_key(org_id), str(user_id))
    except Exception:
        logger.exception("Failed to mark WS user online")


def mark_offline(org_id, user_id):
    try:
        redis_client().srem(online_key(org_id), str(user_id))
    except Exception:
        logger.exception("Failed to mark WS user offline")


def online_user_ids(org_id) -> list[str]:
    try:
        return list(redis_client().smembers(online_key(org_id)) or [])
    except Exception:
        return []


def broadcast_org(org_id, event_type: str, payload: dict):
    layer = get_channel_layer()
    if layer is None:
        return
    message = {"type": event_type, "payload": payload}
    try:
        async_to_sync(layer.group_send)(
            org_group(org_id),
            {"type": "inbox.event", "message": message},
        )
    except Exception:
        logger.exception("Failed to broadcast %s to org %s", event_type, org_id)


def message_ws_payload(msg, contact) -> dict:
    assigned = ""
    if getattr(contact, "assigned_user_id", None):
        assigned = str(contact.assigned_user_id)
    payload = {
        "id": str(msg.id),
        "contact_id": str(contact.id),
        "assigned_user_id": assigned,
        "profile_name": contact.profile_name or "",
        "direction": msg.direction,
        "message_type": msg.message_type,
        "content": {"body": msg.content or ""},
        "media_url": msg.media_url or "",
        "media_mime_type": msg.media_mime_type or "",
        "media_filename": msg.media_filename or "",
        "interactive_data": msg.interactive_data,
        "status": msg.status,
        "wamid": msg.whatsapp_message_id or "",
        "created_at": msg.created_at.isoformat() if msg.created_at else dj_tz.now().isoformat(),
        "updated_at": msg.updated_at.isoformat() if msg.updated_at else dj_tz.now().isoformat(),
        "is_reply": bool(msg.is_reply),
    }
    if msg.is_reply and msg.reply_to_message_id:
        payload["reply_to_message_id"] = str(msg.reply_to_message_id)
        reply = getattr(msg, "reply_to_message", None)
        if reply:
            payload["reply_to_message"] = {
                "id": str(reply.id),
                "content": {"body": reply.content or ""},
                "message_type": reply.message_type,
                "direction": reply.direction,
            }
    return payload
