from django.urls import path

from .hooks import initiate_outgoing_call

urlpatterns = [
    path("calls/outgoing", initiate_outgoing_call),
]
