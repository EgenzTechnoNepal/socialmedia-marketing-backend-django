"""Thin wrapper around the official dodopayments SDK."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


class DodoNotConfigured(RuntimeError):
    pass


def _client():
    if not settings.DODO_PAYMENTS_API_KEY:
        raise DodoNotConfigured("DODO_PAYMENTS_API_KEY is not set")
    from dodopayments import DodoPayments

    kwargs = {"bearer_token": settings.DODO_PAYMENTS_API_KEY}
    env = (settings.DODO_ENVIRONMENT or "test_mode").strip()
    try:
        return DodoPayments(environment=env, **kwargs)
    except TypeError:
        return DodoPayments(**kwargs)


def is_configured() -> bool:
    return bool(settings.DODO_PAYMENTS_API_KEY)


def _catalog_key(*parts: object) -> str:
    """Stable, human-readable identifier sent with catalog create requests."""
    slug = ":".join(str(part).strip().lower() for part in parts)
    return re.sub(r"[^a-z0-9:_-]+", "-", slug)


def _object_id(value: Any, *attrs: str) -> str:
    if isinstance(value, dict):
        return str(next((value.get(attr) for attr in attrs if value.get(attr)), ""))
    return str(next((getattr(value, attr, None) for attr in attrs if getattr(value, attr, None)), ""))


def ensure_plan_product(plan, interval: str) -> str:
    """Return the Dodo subscription product for a plan interval, creating it once."""
    if interval not in ("monthly", "yearly"):
        raise ValueError(f"Unknown billing interval: {interval}")
    model = type(plan)
    id_field = f"dodo_price_id_{interval}"
    sku_field = f"dodo_sku_{interval}"
    with transaction.atomic():
        locked_plan = model.objects.select_for_update().get(pk=plan.pk)
        if not locked_plan.is_paid:
            raise ValueError("Free plans do not have Dodo products")
        price = locked_plan.price_for_interval(interval)
        if price <= 0:
            raise ValueError(f"{locked_plan.key} {interval} price must be greater than zero")
        currency = (locked_plan.currency or "USD").upper()
        sku = _catalog_key("whatomate", "plan", locked_plan.key, interval, currency, price)
        product_id = getattr(locked_plan, id_field)
        current_sku = getattr(locked_plan, sku_field)

        if product_id and not current_sku:
            # Adopt IDs created before SKU signatures were persisted.
            current_sku = sku
            setattr(locked_plan, sku_field, sku)
        if product_id and current_sku == sku:
            locked_plan.dodo_synced_at = timezone.now()
            locked_plan.dodo_sync_error = ""
            locked_plan.save(update_fields=[sku_field, "dodo_synced_at", "dodo_sync_error"])
            setattr(plan, sku_field, sku)
            plan.dodo_synced_at = locked_plan.dodo_synced_at
            plan.dodo_sync_error = ""
            return product_id

        try:
            product = _client().products.create(
                name=f"{locked_plan.name} ({interval.title()})",
                description=locked_plan.description or f"{locked_plan.name} {interval} subscription",
                tax_category="saas",
                price={
                    "type": "recurring_price",
                    "price": price,
                    "currency": currency,
                    "payment_frequency_interval": "Month" if interval == "monthly" else "Year",
                    "payment_frequency_count": 1,
                },
                metadata={"whatomate_sku": sku},
                extra_headers={"Idempotency-Key": sku},
            )
            new_product_id = _object_id(product, "product_id", "id")
            if not new_product_id:
                raise DodoNotConfigured("Dodo did not return a product ID")
        except Exception as exc:
            locked_plan.dodo_sync_error = str(exc)
            locked_plan.save(update_fields=["dodo_sync_error"])
            plan.dodo_sync_error = str(exc)
            raise

        former_ids = list(locked_plan.dodo_former_product_ids or [])
        if product_id and product_id not in former_ids:
            former_ids.append(product_id)
        locked_plan.dodo_former_product_ids = former_ids
        setattr(locked_plan, id_field, new_product_id)
        setattr(locked_plan, sku_field, sku)
        locked_plan.dodo_synced_at = timezone.now()
        locked_plan.dodo_sync_error = ""
        update_fields = [id_field, sku_field, "dodo_former_product_ids", "dodo_synced_at", "dodo_sync_error"]
        if interval == "monthly" and not locked_plan.dodo_product_id:
            locked_plan.dodo_product_id = new_product_id
            update_fields.append("dodo_product_id")
        locked_plan.save(update_fields=update_fields)

    for field in (id_field, sku_field, "dodo_former_product_ids", "dodo_synced_at", "dodo_sync_error"):
        setattr(plan, field, getattr(locked_plan, field))
    if interval == "monthly":
        plan.dodo_product_id = locked_plan.dodo_product_id
    return new_product_id


def ensure_seat_addon(plan, interval: str = "monthly") -> str:
    """Return/create the interval-specific seat add-on for a plan."""
    if interval not in ("monthly", "yearly"):
        raise ValueError(f"Unknown billing interval: {interval}")
    model = type(plan)
    id_field = f"dodo_seat_addon_id_{interval}"
    sku_field = f"dodo_sku_seat_{interval}"
    with transaction.atomic():
        locked_plan = model.objects.select_for_update().get(pk=plan.pk)
        price = locked_plan.extra_seat_price_for_interval(interval)
        currency = (locked_plan.currency or "USD").upper()
        sku = _catalog_key("whatomate", "seat", locked_plan.key, interval, currency, price)
        addon_id = getattr(locked_plan, id_field)
        current_sku = getattr(locked_plan, sku_field)

        if addon_id and not current_sku:
            current_sku = sku
            setattr(locked_plan, sku_field, sku)
        if addon_id and current_sku == sku:
            locked_plan.dodo_synced_at = timezone.now()
            locked_plan.dodo_sync_error = ""
            locked_plan.save(update_fields=[sku_field, "dodo_synced_at", "dodo_sync_error"])
            setattr(plan, sku_field, sku)
            return addon_id

        if not addon_id:
            addon_id = locked_plan.dodo_seat_addon_id or getattr(settings, "DODO_ADDON_SEAT", "")
            if addon_id:
                setattr(locked_plan, id_field, addon_id)
                setattr(locked_plan, sku_field, sku)
                locked_plan.save(update_fields=[id_field, sku_field])
                setattr(plan, id_field, addon_id)
                setattr(plan, sku_field, sku)
                return addon_id
        if price <= 0:
            raise ValueError(f"{locked_plan.key} {interval} seat price must be greater than zero")

        try:
            addon = _client().addons.create(
                name=f"{locked_plan.name} extra seat ({interval})",
                description=f"One extra seat for {locked_plan.name} {interval}; catalog key {sku}",
                currency=currency,
                price=price,
                tax_category="saas",
                extra_headers={"Idempotency-Key": sku},
            )
            new_addon_id = _object_id(addon, "addon_id", "id")
            if not new_addon_id:
                raise DodoNotConfigured("Dodo did not return a seat add-on ID")
        except Exception as exc:
            locked_plan.dodo_sync_error = str(exc)
            locked_plan.save(update_fields=["dodo_sync_error"])
            plan.dodo_sync_error = str(exc)
            raise

        setattr(locked_plan, id_field, new_addon_id)
        setattr(locked_plan, sku_field, sku)
        locked_plan.dodo_synced_at = timezone.now()
        locked_plan.dodo_sync_error = ""
        locked_plan.save(update_fields=[id_field, sku_field, "dodo_synced_at", "dodo_sync_error"])

    for field in (id_field, sku_field, "dodo_synced_at", "dodo_sync_error"):
        setattr(plan, field, getattr(locked_plan, field))
    return new_addon_id


def create_checkout_session(
    *,
    product_id: str,
    extra_seats: int,
    seat_addon_id: str,
    customer_email: str,
    customer_name: str,
    organization_id: str,
    customer_id: str = "",
    return_url: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = _client()
    item: dict[str, Any] = {"product_id": product_id, "quantity": 1}
    if extra_seats:
        if not seat_addon_id:
            raise ValueError("seat_addon_id is required when extra_seats > 0")
        item["addons"] = [{"addon_id": seat_addon_id, "quantity": extra_seats}]
    customer: dict[str, Any]
    if customer_id:
        customer = {"customer_id": customer_id}
    else:
        customer = {"email": customer_email, "name": customer_name or customer_email}
    session_metadata = {**(metadata or {}), "organization_id": organization_id}
    session = client.checkout_sessions.create(
        product_cart=[item],
        customer=customer,
        return_url=return_url or settings.DODO_RETURN_URL,
        metadata=session_metadata,
    )
    return {
        "session_id": getattr(session, "session_id", None) or getattr(session, "id", ""),
        "checkout_url": getattr(session, "checkout_url", "") or getattr(session, "url", ""),
    }


def change_plan(
    *,
    subscription_id: str,
    product_id: str,
    extra_seats: int,
    seat_addon_id: str,
    on_payment_failure: str = "prevent_change",
    proration_billing_mode: str = "prorated_immediately",
) -> Any:
    client = _client()
    addons = []
    if extra_seats and seat_addon_id:
        addons = [{"addon_id": seat_addon_id, "quantity": extra_seats}]
    return client.subscriptions.change_plan(
        subscription_id,
        product_id=product_id,
        quantity=1,
        proration_billing_mode=proration_billing_mode,
        on_payment_failure=on_payment_failure,
        addons=addons,
        
    )
def cancel_subscription(subscription_id: str) -> Any:
    """Schedule cancellation at the end of the current billing period."""
    client = _client()
    return client.subscriptions.update(subscription_id, cancel_at_next_billing_date=True)

def customer_portal_url(customer_id: str, return_url: str = "") -> str:
    client = _client()
    kwargs = {"customer_id": customer_id}
    if return_url:
        kwargs["return_url"] = return_url
    try:
        session = client.customers.customer_portal.create(**kwargs)
    except TypeError:
        session = client.customers.customer_portal.create(customer_id=customer_id)
    link = getattr(session, "link", "")
    if not link:
        raise DodoNotConfigured("Dodo did not return a customer portal link")
    return link

def list_payments(customer_id: str, limit: int = 20) -> list[dict[str, Any]]:
    client = _client()
    payments_api = getattr(client, "payments", None)
    if payments_api is None:
        return []
    try:
        page = payments_api.list(customer_id=customer_id)
    except TypeError:
        page = payments_api.list()
    items = getattr(page, "items", None) or getattr(page, "data", None) or page
    out = []
    for payment in list(items)[:limit]:
        out.append(
            {
                "id": getattr(payment, "payment_id", None) or getattr(payment, "id", ""),
                "status": getattr(payment, "status", ""),
                "amount": getattr(payment, "total_amount", None) or getattr(payment, "amount", None),
                "currency": getattr(payment, "currency", ""),
                "created_at": str(getattr(payment, "created_at", "") or ""),
                "subscription_id": getattr(payment, "subscription_id", "") or "",
            }
        )
    return out


def get_payment_details(payment_id: str) -> dict[str, Any]:
    client = _client()

    payment = client.payments.retrieve(payment_id)
    line_items = client.payments.retrieve_line_items(payment_id)

    return {
        "payment": payment,
        "line_items": line_items,
    }


def ingest_events(events: list[dict[str, Any]]) -> None:
    if not events:
        return
    client = _client()
    usage = getattr(client, "usage_events", None) or getattr(client, "events", None)
    if usage is None:
        raise DodoNotConfigured("SDK has no usage event ingest API")
    ingest = getattr(usage, "ingest", None)
    if ingest is None:
        raise DodoNotConfigured("SDK has no usage_events.ingest")
    ingest(events=events)


def verify_webhook(payload: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """Verify Standard Webhooks signatures. Raises ValueError on failure."""
    secret = settings.DODO_WEBHOOK_SECRET
    if not secret:
        raise ValueError("DODO_WEBHOOK_SECRET is not set")

    try:
        from dodopayments.webhooks import Webhook

        webhook = Webhook(secret)
        event = webhook.verify(payload, headers)
        if isinstance(event, dict):
            return event
        return {"type": getattr(event, "type", ""), "data": getattr(event, "data", event)}
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("dodopayments.webhooks verify failed, trying unwrap: %s", exc)

    try:
        client = _client()
        unwrap = getattr(getattr(client, "webhooks", None), "unwrap", None)
        if unwrap:
            event = unwrap(payload, headers)
            if isinstance(event, dict):
                return event
    except Exception as exc:
        logger.warning("SDK webhook unwrap failed: %s", exc)

    # Fallback: HMAC over Standard Webhooks msg_id.timestamp.body
    import hashlib
    import hmac

    msg_id = headers.get("webhook-id") or headers.get("Webhook-Id") or ""
    timestamp = headers.get("webhook-timestamp") or headers.get("Webhook-Timestamp") or ""
    signature = headers.get("webhook-signature") or headers.get("Webhook-Signature") or ""
    if not (msg_id and timestamp and signature):
        raise ValueError("Missing webhook signature headers")
    try:
        timestamp_value = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid webhook timestamp") from exc
    tolerance = int(getattr(settings, "DODO_WEBHOOK_TOLERANCE_SECONDS", 300))
    if abs(time.time() - timestamp_value) > tolerance:
        raise ValueError("Webhook timestamp is outside the allowed tolerance")
    signed = f"{msg_id}.{timestamp}.".encode() + payload
    key = secret
    if secret.startswith("whsec_"):
        import base64

        key = base64.b64decode(secret[len("whsec_") :])
    else:
        key = secret.encode()
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    import base64

    expected = "v1," + base64.b64encode(digest).decode()
    parts = [p.strip() for p in signature.split()]
    if not any(hmac.compare_digest(p, expected) for p in parts):
        # Some payloads use v1=<base64>
        expected_eq = "v1=" + base64.b64encode(digest).decode()
        if not any(hmac.compare_digest(p, expected_eq) for p in parts):
            raise ValueError("Invalid webhook signature")
    import json

    return json.loads(payload.decode("utf-8"))
