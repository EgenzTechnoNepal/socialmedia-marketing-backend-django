from django.urls import path

from . import hooks

urlpatterns = [
    path("calls/outgoing/<uuid:call_log_id>/hangup", hooks.not_ported),
    path("calls/outgoing", hooks.initiate_outgoing_call),
    path("calls/permission-request", hooks.not_ported),
    path("calls/permission/<uuid:contact_id>", hooks.call_permission),
    path("calls/ice-servers", hooks.ice_servers),
    path("ivr-flows", hooks.not_ported),
    path("call-logs", hooks.not_ported),
    path("call-transfers", hooks.not_ported),
]
