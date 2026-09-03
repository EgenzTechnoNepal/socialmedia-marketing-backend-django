from django.contrib import admin

from .models import BillingPlan, DodoWebhookEvent, OrganizationSubscription, UsageCounter, UsageEventOutbox


@admin.register(BillingPlan)
class BillingPlanAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "included_seats", "dodo_product_id", "is_active")


@admin.register(OrganizationSubscription)
class OrganizationSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("organization", "plan", "status", "extra_seats", "dodo_subscription_id")


@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    list_display = ("organization", "meter", "period_start", "quantity")


@admin.register(UsageEventOutbox)
class UsageEventOutboxAdmin(admin.ModelAdmin):
    list_display = ("event_id", "event_name", "organization", "ingested_at", "attempts")


@admin.register(DodoWebhookEvent)
class DodoWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("webhook_id", "event_type", "processed_at")
