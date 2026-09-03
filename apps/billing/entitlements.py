from __future__ import annotations

from django.db import transaction
from django.db.models import F
from django.utils import timezone as dj_tz

from apps.accounts.models import Organization, User
from apps.common.exceptions import EntitlementError

from .models import (
    DEFAULT_FEATURES,
    DEFAULT_QUOTAS,
    METER_AI_COMPLETION,
    METER_CAMPAIGN_RECIPIENT,
    METER_MESSAGE_SENT,
    PLAN_FREE,
    STATUS_FREE,
    BillingPlan,
    OrganizationSubscription,
    UsageCounter,
    UsageEventOutbox,
)

FEATURE_CAMPAIGNS = "campaigns"
FEATURE_AI = "ai"
FEATURE_CALLING = "calling"
FEATURE_EXTRA_WA = "extra_wa_accounts"

METER_ALIASES = {
    METER_MESSAGE_SENT: METER_MESSAGE_SENT,
    METER_AI_COMPLETION: METER_AI_COMPLETION,
    METER_CAMPAIGN_RECIPIENT: METER_CAMPAIGN_RECIPIENT,
}


def period_start(now=None):
    now = now or dj_tz.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def get_or_create_subscription(organization_id) -> OrganizationSubscription:
    org = Organization.objects.get(id=organization_id)
    sub = OrganizationSubscription.objects.filter(organization=org).select_related("plan").first()
    if sub:
        return sub
    plan, _ = BillingPlan.objects.get_or_create(
        key=PLAN_FREE,
        defaults={
            "name": "Free",
            "description": "Starter plan",
            "included_seats": 1,
            "features": DEFAULT_FEATURES[PLAN_FREE],
            "included_quotas": DEFAULT_QUOTAS[PLAN_FREE],
            "display_order": 0,
        },
    )
    return OrganizationSubscription.objects.create(organization=org, plan=plan, status=STATUS_FREE)


def seats_used(organization_id) -> int:
    return User.objects.filter(organization_id=organization_id, is_active=True).count()


def assert_can_add_seat(organization_id) -> OrganizationSubscription:
    sub = get_or_create_subscription(organization_id)
    if not sub.is_usable:
        raise EntitlementError("Subscription is not active. Update billing to add agents.")
    used = seats_used(organization_id)
    if used >= sub.seat_limit:
        raise EntitlementError(
            f"Seat limit reached ({used}/{sub.seat_limit}). Purchase extra seats in Billing."
        )
    return sub


def assert_feature(organization_id, feature: str) -> OrganizationSubscription:
    sub = get_or_create_subscription(organization_id)
    if not sub.is_usable and feature != "chat_read":
        raise EntitlementError("Subscription is on hold or cancelled. Outbound features are disabled.")
    if not sub.feature_enabled(feature):
        raise EntitlementError(f"Your {sub.plan.name} plan does not include {feature}. Upgrade in Billing.")
    return sub


def assert_outbound_allowed(organization_id) -> OrganizationSubscription:
    sub = get_or_create_subscription(organization_id)
    if not sub.is_usable:
        raise EntitlementError("Subscription is not active. Outbound messaging is disabled.")
    return sub


def quota_for(sub: OrganizationSubscription, meter: str) -> int:
    quotas = sub.plan.included_quotas or DEFAULT_QUOTAS.get(sub.plan.key, {})
    return int(quotas.get(meter, 0))


def current_usage(organization_id, meter: str) -> int:
    start = period_start()
    row = UsageCounter.objects.filter(
        organization_id=organization_id, meter=meter, period_start=start
    ).first()
    return int(row.quantity) if row else 0


def record_usage(organization_id, meter: str, *, event_id: str, quantity: int = 1, metadata=None) -> int:
    """Increment local counter, optionally hard-cap free plans, enqueue Dodo ingest."""
    meter = METER_ALIASES.get(meter, meter)
    sub = get_or_create_subscription(organization_id)
    if not sub.is_usable:
        raise EntitlementError("Subscription is not active.")

    included = quota_for(sub, meter)
    features = sub.plan.features or DEFAULT_FEATURES.get(sub.plan.key, {})
    hard_cap = bool(features.get("hard_cap_usage"))
    start = period_start()

    with transaction.atomic():
        counter, _ = UsageCounter.objects.select_for_update().get_or_create(
            organization_id=organization_id,
            meter=meter,
            period_start=start,
            defaults={"quantity": 0},
        )
        if hard_cap and included >= 0 and counter.quantity + quantity > included:
            raise EntitlementError(
                f"Included {meter} quota reached ({counter.quantity}/{included}) for this period."
            )
        UsageCounter.objects.filter(pk=counter.pk).update(quantity=F("quantity") + quantity)
        counter.refresh_from_db()

        if sub.plan.is_paid and sub.dodo_customer_id:
            UsageEventOutbox.objects.get_or_create(
                event_id=event_id,
                defaults={
                    "organization_id": organization_id,
                    "event_name": meter,
                    "quantity": quantity,
                    "metadata": metadata or {},
                },
            )

    from .tasks import ingest_usage_events

    ingest_usage_events.delay()
    return counter.quantity
