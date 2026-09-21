from django.contrib import admin

from .models import BillingPayment, BillingPlan, DodoWebhookEvent, OrganizationSubscription, UsageCounter, UsageEventOutbox,PaymentProfile


@admin.register(BillingPlan)
class BillingPlanAdmin(admin.ModelAdmin):
    list_display = (
        "key", "name", "included_seats",
        "dodo_product_id", "dodo_price_id_monthly", "dodo_price_id_yearly",
        "is_active",
    )


@admin.register(OrganizationSubscription)
class OrganizationSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "organization", "plan", "status", "billing_interval",
        "extra_seats", "cancel_at_period_end", "dodo_subscription_id",
    )
    list_filter = ("status", "billing_interval", "cancel_at_period_end")
@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    list_display = ("organization", "meter", "period_start", "quantity")


@admin.register(UsageEventOutbox)
class UsageEventOutboxAdmin(admin.ModelAdmin):
    list_display = ("event_id", "event_name", "organization", "ingested_at", "attempts")


@admin.register(DodoWebhookEvent)
class DodoWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("webhook_id", "event_type", "processed_at")


@admin.register(BillingPayment)
class BillingPaymentAdmin(admin.ModelAdmin):
    list_display = ("payment_id", "organization", "status", "refund_status", "amount", "currency")
    list_filter = ("status", "refund_status", "currency")

@admin.register(PaymentProfile)
class PaymentProfileAdmin(admin.ModelAdmin):
    list_display = ("organization", "billing_name", "billing_email", "country", "tax_id")