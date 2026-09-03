from django.urls import path

from .hooks import send_message

urlpatterns = [
    path("contacts/<uuid:contact_id>/messages", send_message),
    path("messages", send_message),
    path("messages/template", send_message),
    path("messages/media", send_message),
]
