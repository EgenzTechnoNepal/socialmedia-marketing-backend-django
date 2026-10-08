from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import IntegrityError, transaction
from apps.accounts.models import Organization

from .models import (
    INTERVAL_MONTHLY,
    INTERVAL_YEARLY,
    PLAN_FREE,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_FREE,
    STATUS_ON_HOLD,
    BillingPayment,
    BillingPlan,
    DodoWebhookEvent,
    OrganizationSubscription,
)
from .services import dodo

logger = logging.getLogger(__name__)

HANDLED = {
    "subscription.active",
    "subscription.plan_changed",
    "subscription.renewed",
    "subscription.on_hold",
    "subscription.cancelled",
    "subscription.expired",
    "subscription.failed",
    "payment.succeeded",
    "payment.failed",
    "refund.succeeded",
    "refund.failed",
}


def _payment_amount(data: dict, key: str = "amount") -> int | None:
    value = data.get(key)
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _payment_customer_id(data: dict) -> str:
    customer = data.get("customer") or {}
    if isinstance(customer, dict):
        return str(customer.get("customer_id") or customer.get("id") or "")
    return str(data.get("customer_id") or "")


def _upsert_payment(data: dict, event_type: str):
    payment_id = str(data.get("payment_id") or data.get("id") or "")
    if not payment_id:
        logger.warning("Dodo payment event has no payment id: %s", event_type)
        return

    subscription_id = str(data.get("subscription_id") or "")
    customer_id = _payment_customer_id(data)
    sub = None
    if subscription_id:
        sub = OrganizationSubscription.objects.filter(
            dodo_subscription_id=subscription_id
        ).first()
    if sub is None and customer_id:
        sub = OrganizationSubscription.objects.filter(
            dodo_customer_id=customer_id
        ).first()

    organization = sub.organization if sub else None
    if organization is None:
        organization_id = _org_id(data)
        if organization_id:
            organization = Organization.objects.filter(id=organization_id).first()

    BillingPayment.objects.update_or_create(
        payment_id=payment_id,
        defaults={
            "organization": organization,
            "subscription_id": subscription_id,
            "customer_id": customer_id,
            "status": "succeeded" if event_type == "payment.succeeded" else "failed",
            "amount": _payment_amount(data, "total_amount") or _payment_amount(data),
            "currency": str(data.get("currency") or "")[:3].upper(),
            "payload": data,
            "occurred_at": _parse_dt(data.get("created_at") or data.get("timestamp")),
        },
    )
    if sub and event_type == "payment.failed" and sub.status == STATUS_ACTIVE:
        sub.status = STATUS_FAILED
        sub.save(update_fields=["status", "updated_at"])


def _upsert_refund(data: dict, event_type: str):
    payment_id = str(data.get("payment_id") or data.get("id") or "")
    if not payment_id:
        logger.warning("Dodo refund event has no payment id: %s", event_type)
        return
    payment = BillingPayment.objects.filter(payment_id=payment_id).first()
    if payment is None:
        payment = BillingPayment.objects.create(
            payment_id=payment_id,
            status="unknown",
            payload={},
        )
    payment.refund_status = "succeeded" if event_type == "refund.succeeded" else "failed"
    payment.refund_amount = _payment_amount(data, "amount") or _payment_amount(data, "refund_amount")
    payment.payload = {**(payment.payload or {}), "last_refund_event": data}
    payment.occurred_at = _parse_dt(data.get("created_at") or data.get("timestamp")) or payment.occurred_at
    payment.save(update_fields=["refund_status", "refund_amount", "payload", "occurred_at", "updated_at"])


def _process_event(event_type: str, data: dict):
    if event_type in {"subscription.active", "subscription.renewed", "subscription.plan_changed"}:
        _upsert_subscription(data, STATUS_ACTIVE)
    elif event_type == "subscription.on_hold":
        _upsert_subscription(data, STATUS_ON_HOLD)
    elif event_type in {"subscription.cancelled", "subscription.expired"}:
        _upsert_subscription(data, STATUS_CANCELLED)
    elif event_type == "subscription.failed":
        _upsert_subscription(data, STATUS_FAILED)
    elif event_type in {"payment.succeeded", "payment.failed"}:
        _upsert_payment(data, event_type)
    elif event_type in {"refund.succeeded", "refund.failed"}:
        _upsert_refund(data, event_type)

def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    parsed = parse_datetime(str(value).replace("Z", "+00:00"))
    return parsed


def _data(event: dict) -> dict:
    data = event.get("data") or event
    if not isinstance(data, dict):
        return {}
    return data


def _org_id(data: dict) -> str | None:
    meta = data.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("organization_id"):
        return str(meta["organization_id"])
    return None


def _plan_for_product(product_id: str) -> tuple[BillingPlan | None, str | None]:
    if not product_id:
        return None, None
    plan = BillingPlan.objects.filter(dodo_price_id_monthly=product_id).first()
    if plan:
        return plan, INTERVAL_MONTHLY
    plan = BillingPlan.objects.filter(dodo_price_id_yearly=product_id).first()
    if plan:
        return plan, INTERVAL_YEARLY
    plan = BillingPlan.objects.filter(dodo_product_id=product_id).first()
    if plan:
        return plan, INTERVAL_MONTHLY
    return None, None


def _extra_seats(data: dict, plan: BillingPlan | None = None) -> int:
    addons = data.get("addons") or data.get("add_ons") or []
    seat_ids = {settings.DODO_ADDON_SEAT}
    if plan:
        seat_ids.update(
            {
                plan.dodo_seat_addon_id,
                plan.dodo_seat_addon_id_monthly,
                plan.dodo_seat_addon_id_yearly,
            }
        )
    seat_ids.discard("")
    if not seat_ids:
        return 0
    total = 0
    for addon in addons:
        if not isinstance(addon, dict):
            continue
        addon_id = addon.get("addon_id") or addon.get("id") or ""
        try:
            qty = int(addon.get("quantity") or 0)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid Dodo seat addon quantity: %r", addon.get("quantity"))
            continue
        if addon_id in seat_ids:
            total += qty
    return total


def _upsert_subscription(data: dict, status: str):
    sub_id = data.get("subscription_id") or data.get("id") or ""
    customer_id = ""
    customer = data.get("customer") or {}
    if isinstance(customer, dict):
        customer_id = customer.get("customer_id") or customer.get("id") or ""
    customer_id = customer_id or data.get("customer_id") or ""
    product_id = data.get("product_id") or ""
    if not product_id and isinstance(data.get("product"), dict):
        product_id = data["product"].get("product_id") or data["product"].get("id") or ""

    org_id = _org_id(data)
    sub = None
    if sub_id:
        sub = OrganizationSubscription.objects.filter(dodo_subscription_id=sub_id).select_related("plan").first()
    if sub is None and org_id:
        sub = OrganizationSubscription.objects.filter(organization_id=org_id).select_related("plan").first()
    if sub is None and org_id:
        org = Organization.objects.filter(id=org_id).first()
        if not org:
            logger.warning("Dodo webhook org not found: %s", org_id)
            return
        free = BillingPlan.objects.filter(key=PLAN_FREE).first()
        sub = OrganizationSubscription.objects.create(organization=org, plan=free, status=STATUS_FREE)

    if sub is None:
        logger.warning("Dodo webhook could not map subscription: %s", sub_id)
        return

    plan, interval = _plan_for_product(product_id)
    if plan:
        sub.plan = plan
        sub.billing_interval = interval
        if sub.pending_plan_id and sub.pending_plan_id == plan.id:
            sub.pending_plan = None
            sub.pending_billing_interval = None
    if customer_id:
        sub.dodo_customer_id = customer_id
    if sub_id:
        sub.dodo_subscription_id = sub_id
    extra = _extra_seats(data, plan)
    if extra or status in {STATUS_ACTIVE, STATUS_ON_HOLD}:
        sub.extra_seats = extra
    sub.status = status
    sub.current_period_start = _parse_dt(data.get("previous_billing_date") or data.get("created_at")) or sub.current_period_start
    sub.current_period_end = _parse_dt(data.get("next_billing_date")) or sub.current_period_end
    if "cancel_at_next_billing_date" in data:
        sub.cancel_at_period_end = bool(data["cancel_at_next_billing_date"])
    if status == STATUS_CANCELLED:
        free_plan = BillingPlan.objects.filter(key=PLAN_FREE).first()
        if free_plan:
            sub.plan = free_plan
        sub.extra_seats = 0
        sub.cancel_at_period_end = False
        sub.pending_plan = None
        sub.pending_billing_interval = None

    sub.save()


@csrf_exempt
@require_POST
def dodo_webhook(request):
    try:
        event = dodo.verify_webhook(request.body, {k.lower(): v for k, v in request.headers.items()})
    except ValueError as exc:
        logger.warning("Dodo webhook rejected: %s", exc)
        return JsonResponse({"status": "error", "message": str(exc)}, status=401)
    except Exception:
        logger.exception("Dodo webhook verification error")
        return JsonResponse({"status": "error", "message": "invalid signature"}, status=401)

    webhook_id = request.headers.get("webhook-id") or ""
    if not webhook_id:
        return JsonResponse({"status": "error", "message": "missing webhook-id"}, status=400)
    event_type = event.get("type") or ""

    try:
        with transaction.atomic():
            try:
                with transaction.atomic():
                    DodoWebhookEvent.objects.create(
                        webhook_id=webhook_id, event_type=event_type, payload=event
                    )
            except IntegrityError:
                return JsonResponse({"status": "success", "data": {"duplicate": True}})

            if event_type in HANDLED:
                _process_event(event_type, _data(event))
    except Exception:
        logger.exception("Dodo webhook handler failed for %s", event_type)
        return JsonResponse({"status": "error", "message": "handler failed"}, status=500)

    return JsonResponse({"status": "success", "data": {"type": event_type}})
