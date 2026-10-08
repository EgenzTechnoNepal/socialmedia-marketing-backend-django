import uuid

from django.db import models

PLAN_FREE = "free"
PLAN_PRO = "pro"
PLAN_BUSINESS = "business"

PLAN_KEYS = (PLAN_FREE, PLAN_PRO, PLAN_BUSINESS)

STATUS_FREE = "free"
STATUS_ACTIVE = "active"
STATUS_ON_HOLD = "on_hold"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

INTERVAL_MONTHLY="monthly"
INTERVAL_YEARLY="yearly"
BILLING_INTERVALS=(INTERVAL_MONTHLY,INTERVAL_YEARLY)

METER_MESSAGE_SENT = "message.sent"
METER_AI_COMPLETION = "ai.completion"
METER_CAMPAIGN_RECIPIENT = "campaign.recipient"

DEFAULT_FEATURES = {
    PLAN_FREE: {
        "campaigns": False,
        "ai": False,
        "calling": False,
        "extra_wa_accounts": False,
        "hard_cap_usage": True,
        "teams_basic":False,
        "teams_advanced":False,
        "custom_roles":False,
        "api_keys":False,
        "webhooks":False,
        "custom_actions":False,
        "audit_logs":False,
        "sso":False,

    },
    PLAN_PRO: {
        "campaigns": True,
        "ai": True,
        "calling": True,
        "extra_wa_accounts": True,
        "teams_basic": True,
        "hard_cap_usage": False,
        "teams_advanced":False,
        "custom_roles": False,
        "api_keys": False,
        "webhooks": False,
        "custom_actions":False,
        "audit_logs": False,
        "sso": False
    },
    PLAN_BUSINESS: {
        "campaigns": True,
        "ai": True,
        "calling": True,
        "extra_wa_accounts": True,
        "hard_cap_usage": False,
        "teams_basic": True,
        "teams_advanced":True,
        "custom_roles": True,
        "api_keys": True,
        "webhooks": True,
        "custom_actions":True,
        "audit_logs": True,
        "sso": True,
    },
}

DEFAULT_QUOTAS = {
    PLAN_FREE: {
        METER_MESSAGE_SENT: 100,
        METER_AI_COMPLETION: 0,
        METER_CAMPAIGN_RECIPIENT: 0,
    },
    PLAN_PRO: {
        METER_MESSAGE_SENT: 5000,
        METER_AI_COMPLETION: 1000,
        METER_CAMPAIGN_RECIPIENT: 5000,
    },
    PLAN_BUSINESS: {
        METER_MESSAGE_SENT: 25000,
        METER_AI_COMPLETION: 10000,
        METER_CAMPAIGN_RECIPIENT: 25000,
    },
}


class BillingPlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    currency = models.CharField(max_length=3, default="USD")
    price_monthly = models.PositiveIntegerField(default=0)  # minor units (e.g. cents)
    price_yearly = models.PositiveIntegerField(default=0)   # minor units (e.g. cents)

    dodo_product_id = models.CharField(max_length=128, blank=True)
    dodo_price_id_monthly = models.CharField(max_length=128, blank=True)
    dodo_price_id_yearly = models.CharField(max_length=128, blank=True)
    dodo_sku_monthly = models.CharField(max_length=255, blank=True)
    dodo_sku_yearly = models.CharField(max_length=255, blank=True)
    dodo_seat_addon_id = models.CharField(max_length=128, blank=True)
    dodo_seat_addon_id_monthly = models.CharField(max_length=128, blank=True)
    dodo_seat_addon_id_yearly = models.CharField(max_length=128, blank=True)
    dodo_sku_seat_monthly = models.CharField(max_length=255, blank=True)
    dodo_sku_seat_yearly = models.CharField(max_length=255, blank=True)
    dodo_synced_at = models.DateTimeField(null=True, blank=True)
    dodo_sync_error = models.TextField(blank=True)
    dodo_former_product_ids = models.JSONField(default=list, blank=True)
    extra_seat_price_monthly = models.PositiveIntegerField(default=0)
    extra_seat_price_yearly = models.PositiveIntegerField(default=0)

    included_seats = models.PositiveIntegerField(default=1)
    included_wa_accounts = models.PositiveIntegerField(default=1)
    features = models.JSONField(default=dict)
    included_quotas = models.JSONField(default=dict)
    display_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_plans"
        ordering = ["display_order", "key"]

    def __str__(self):
        return self.key
    
    #this checks if it is a free plan or not
    @property
    def is_paid(self):
        return self.key != PLAN_FREE
    
    @property
    def is_checkout_ready(self):
        return self.is_paid and (
            (self.price_monthly > 0 and bool(self.dodo_price_id_monthly))
            or (self.price_yearly > 0 and bool(self.dodo_price_id_yearly))
        )

    def is_checkout_ready_for_interval(self, interval: str) -> bool:
        return self.is_paid and self.price_for_interval(interval) > 0 and bool(
            self.dodo_price_id_for_interval(interval)
        )

    def price_for_interval(self, interval: str) -> int:
        if interval == INTERVAL_YEARLY:
            return self.price_yearly
        elif interval == INTERVAL_MONTHLY:
            return self.price_monthly
        raise ValueError(f"Unknown billing interval: {interval}")

    def dodo_price_id_for_interval(self, interval: str) -> str:
        if interval == INTERVAL_YEARLY:
            return self.dodo_price_id_yearly
        elif interval == INTERVAL_MONTHLY:
            return self.dodo_price_id_monthly
        raise ValueError(f"Unknown billing interval: {interval}")
    
    def dodo_product_id_for_interval(self, interval: str) -> str:
        """Dodo sells monthly and yearly as two separate subscription products."""
        return self.dodo_price_id_for_interval(interval)
    
    def extra_seat_price_for_interval(self, interval: str) -> int:
        if interval == INTERVAL_YEARLY:
            return self.extra_seat_price_yearly
        elif interval == INTERVAL_MONTHLY:
            return self.extra_seat_price_monthly
        raise ValueError(f"Unknown billing interval: {interval}")
class OrganizationSubscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="subscription",
        db_constraint=False,
    )
    plan = models.ForeignKey(BillingPlan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=32, default=STATUS_FREE)
    billing_interval = models.CharField(
        max_length=16, choices=[(i, i) for i in BILLING_INTERVALS], default=INTERVAL_MONTHLY
    )
    dodo_customer_id = models.CharField(max_length=128, blank=True)
    dodo_subscription_id = models.CharField(max_length=128, blank=True)
    extra_seats = models.PositiveIntegerField(default=0)

    pending_plan = models.ForeignKey(
        BillingPlan, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pending_subscriptions",
    )
    pending_billing_interval = models.CharField(
        max_length=16, choices=[(i, i) for i in BILLING_INTERVALS], null=True, blank=True
    )
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organization_subscriptions"

    @property
    def seat_limit(self):
        return int(self.plan.included_seats) + int(self.extra_seats)

    @property
    def is_usable(self):
        return self.status in {STATUS_FREE, STATUS_ACTIVE}

    def feature_enabled(self, name: str) -> bool:
        features = self.plan.features or DEFAULT_FEATURES.get(self.plan.key, {})
        return bool(features.get(name))


class UsageCounter(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "accounts.Organization",
        on_delete=models.CASCADE,
        db_constraint=False,
        related_name="usage_counters",
    )
    meter = models.CharField(max_length=64)
    period_start = models.DateTimeField()
    quantity = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_usage_counters"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "meter", "period_start"],
                name="uniq_usage_org_meter_period",
            )
        ]


class UsageEventOutbox(models.Model):
    """Queued Dodo usage events. Celery batches ingest; unique event_id is the idempotency key."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "accounts.Organization",
        on_delete=models.CASCADE,
        db_constraint=False,
        related_name="usage_outbox",
    )
    event_id = models.CharField(max_length=128, unique=True)
    event_name = models.CharField(max_length=64)
    quantity = models.PositiveIntegerField(default=1)
    metadata = models.JSONField(default=dict, blank=True)
    ingested_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_usage_outbox"
        indexes = [
            models.Index(fields=["ingested_at", "created_at"], name="idx_usage_outbox_pending"),
        ]


class DodoWebhookEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    webhook_id = models.CharField(max_length=128, unique=True)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField(default=dict)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "dodo_webhook_events"


class BillingPayment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "accounts.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_constraint=False,
        related_name="billing_payments",
    )
    payment_id = models.CharField(max_length=128, unique=True)
    subscription_id = models.CharField(max_length=128, blank=True)
    customer_id = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=32)
    amount = models.PositiveIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    refund_status = models.CharField(max_length=32, blank=True)
    refund_amount = models.PositiveIntegerField(null=True, blank=True)
    payload = models.JSONField(default=dict)
    occurred_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_payments"


class PaymentProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.OneToOneField(
    "accounts.Organization",
    on_delete=models.CASCADE,
    related_name="payment_profile",
    db_constraint=False,
    )

    billing_name = models.CharField(max_length=255)
    billing_email = models.EmailField()
    phone = models.CharField(max_length=32, blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=32, blank=True)
    country = models.CharField(max_length=100, blank=True)
    tax_id = models.CharField(max_length=100, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payment_profiles"

    def __str__(self):
        return f"Payment Profile - {self.billing_email}"
