from django.urls import path

from . import views

urlpatterns = [
    path("campaigns/<uuid:campaign_id>/recipients/import", views.import_recipients),
    path("campaigns/<uuid:campaign_id>/recipients/<uuid:recipient_id>", views.delete_recipient),
    path("campaigns/<uuid:campaign_id>/recipients", views.campaign_recipients),
    path("campaigns/<uuid:campaign_id>/start", views.start_campaign),
    path("campaigns/<uuid:campaign_id>/pause", views.pause_campaign),
    path("campaigns/<uuid:campaign_id>/cancel", views.cancel_campaign),
    path("campaigns/<uuid:campaign_id>/retry-failed", views.retry_failed),
    path("campaigns/<uuid:campaign_id>/progress", views.campaign_progress),
    path("campaigns/<uuid:campaign_id>/media", views.campaign_media),
    path("campaigns/<uuid:campaign_id>", views.campaign_detail),
    path("campaigns", views.campaigns_collection),
]
