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
    },
    PLAN_PRO: {
        "campaigns": True,
        "ai": True,
        "calling": True,
        "extra_wa_accounts": True,
        "hard_cap_usage": False,
    },
    PLAN_BUSINESS: {
        "campaigns": True,
        "ai": True,
        "calling": True,
        "extra_wa_accounts": True,
        "hard_cap_usage": False,
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
    dodo_product_id = models.CharField(max_length=128, blank=True)
    dodo_seat_addon_id = models.CharField(max_length=128, blank=True)
    included_seats = models.PositiveIntegerField(default=1)
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

    @property
    def is_paid(self):
        return self.key != PLAN_FREE and bool(self.dodo_product_id)


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
    dodo_customer_id = models.CharField(max_length=128, blank=True)
    dodo_subscription_id = models.CharField(max_length=128, blank=True)
    extra_seats = models.PositiveIntegerField(default=0)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
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
