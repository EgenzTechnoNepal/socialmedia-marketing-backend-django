import json
import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

from apps.common.tokens import decode_token
from apps.realtime.hub import mark_offline, mark_online, org_group

logger = logging.getLogger(__name__)


class InboxConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.org_id = None
        self.user_id = None
        self.contact_id = ""
        await self.accept()

    async def disconnect(self, code):
        if self.org_id:
            await self.channel_layer.group_discard(org_group(self.org_id), self.channel_name)
            if self.user_id:
                await self._offline()

    async def receive_json(self, content, **kwargs):
        msg_type = content.get("type")
        payload = content.get("payload") or {}
        if msg_type == "auth":
            await self._auth(payload.get("token") or "")
            return
        if not self.user_id:
            await self.close(code=4001)
            return
        if msg_type == "ping":
            await self.send_json({"type": "pong", "payload": {}})
            return
        if msg_type == "set_contact":
            self.contact_id = payload.get("contact_id") or ""

    async def inbox_event(self, event):
        await self.send_json(event["message"])

    async def _auth(self, token: str):
        try:
            claims = decode_token(token, verify_exp=True)
        except Exception:
            await self.close(code=4001)
            return
        if claims.get("sub") != "ws":
            await self.close(code=4001)
            return
        self.user_id = claims.get("user_id")
        self.org_id = claims.get("organization_id")
        if not self.user_id or not self.org_id:
            await self.close(code=4001)
            return
        await self.channel_layer.group_add(org_group(self.org_id), self.channel_name)
        await self._online()

    async def _online(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(mark_online)(self.org_id, self.user_id)

    async def _offline(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(mark_offline)(self.org_id, self.user_id)
