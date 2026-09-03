from django.urls import path

from . import views, webhook, flows

urlpatterns = [
    path("accounts/<uuid:account_id>", views.account_detail),
    path("accounts", views.accounts_collection),
    path("webhook", webhook.webhook_receive),
    path("flows/sync", flows.sync_flows),
    path("flows/<uuid:flow_id>/save-to-meta", flows.save_flow_to_meta),
    path("flows/<uuid:flow_id>/publish", flows.publish_flow),
    path("flows/<uuid:flow_id>/deprecate", flows.deprecate_flow),
    path("flows/<uuid:flow_id>/duplicate", flows.duplicate_flow),
    path("flows/<uuid:flow_id>", flows.flow_detail),
    path("flows", flows.flows_collection),
]
