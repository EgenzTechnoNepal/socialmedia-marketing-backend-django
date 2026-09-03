from django.contrib import admin
from django.urls import include, path

from apps.billing.webhooks import dodo_webhook
from apps.common.views import health, ready

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", health),
    path("ready", ready),
    path("api/webhooks/dodo", dodo_webhook),
    path("api/billing/", include("apps.billing.urls")),
    path("api/", include("apps.accounts.urls")),
    path("api/", include("apps.contacts.urls")),
    path("api/", include("apps.whatsapp.urls")),
    path("api/", include("apps.campaigns.urls")),
    path("api/", include("apps.chatbot.urls")),
    path("api/", include("apps.webhooks.urls")),
    path("api/", include("apps.calling.urls")),
    path("api/", include("apps.catalogs.urls")),
    path("api/", include("apps.analytics.urls")),
]
