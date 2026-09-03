from django.urls import re_path

from apps.realtime.consumers import InboxConsumer

websocket_urlpatterns = [
    re_path(r"^ws$", InboxConsumer.as_asgi()),
]
