"""Thin wrapper around the official dodopayments SDK."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

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
) -> dict[str, Any]:
    client = _client()
    item: dict[str, Any] = {"product_id": product_id, "quantity": 1}
    if extra_seats and seat_addon_id:
        item["addons"] = [{"addon_id": seat_addon_id, "quantity": extra_seats}]
    customer: dict[str, Any]
    if customer_id:
        customer = {"customer_id": customer_id}
    else:
        customer = {"email": customer_email, "name": customer_name or customer_email}
    session = client.checkout_sessions.create(
        product_cart=[item],
        customer=customer,
        return_url=return_url or settings.DODO_RETURN_URL,
        metadata={"organization_id": organization_id},
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
        addons=addons,
    )


def customer_portal_url(customer_id: str, return_url: str = "") -> str:
    client = _client()
    customers = getattr(client, "customers", None)
    if customers is None:
        raise DodoNotConfigured("SDK has no customers API")
    for method_name in ("customer_portal", "create_customer_portal", "portal"):
        method = getattr(customers, method_name, None)
        if method is None:
            continue
        try:
            result = method(customer_id)
        except TypeError:
            result = method(customer_id=customer_id, return_url=return_url or settings.DODO_RETURN_URL)
        return (
            getattr(result, "link", None)
            or getattr(result, "portal_url", None)
            or getattr(result, "url", None)
            or str(result)
        )
    raise DodoNotConfigured("Could not create a Dodo customer portal session")


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
