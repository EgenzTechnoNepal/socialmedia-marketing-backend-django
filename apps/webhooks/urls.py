from django.urls import path

from . import views

urlpatterns = [
    path("webhooks/<uuid:webhook_id>/test", views.test_webhook),
    path("webhooks/<uuid:webhook_id>", views.webhook_detail),
    path("webhooks", views.webhooks_collection),
    path("custom-actions/redirect/<str:token>", views.custom_action_redirect),
    path("custom-actions/<uuid:action_id>/execute", views.execute_custom_action),
    path("custom-actions/<uuid:action_id>", views.custom_action_detail),
    path("custom-actions", views.custom_actions_collection),
]
