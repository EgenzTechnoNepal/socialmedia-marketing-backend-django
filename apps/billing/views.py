from __future__ import annotations
from datetime import datetime, timezone

from django.utils import timezone as dj_tz
import logging

from django.conf import settings
from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes

from .serializers import PaymentProfileSerializer
from django.urls import reverse
from apps.common.envelope import error, success
from apps.common.exceptions import APIError
from apps.common.permissions import (
    CookieAuthenticated,
    HasOrg,
    HasBillingAccess,
    request_organization_id,
)
from apps.common.http import list_payload, parse_optional_date_range, parse_pagination
from .entitlements import (
    METER_AI_COMPLETION,
    METER_CAMPAIGN_RECIPIENT,
    METER_MESSAGE_SENT,
    current_usage,
    get_or_create_subscription,
    period_start,
    quota_for,
    seats_used,
    whatsapp_accounts_used,
)
from .models import (
    BILLING_INTERVALS,
    INTERVAL_MONTHLY,
    INTERVAL_YEARLY,
    PLAN_FREE,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_ON_HOLD,
    BillingPlan,
    OrganizationSubscription,
    PaymentProfile,
)
from .services import dodo
from .services.dodo import DodoNotConfigured
from .services.invoice import build_invoice_data, generate_invoice_pdf

logger = logging.getLogger(__name__)

METERS = (METER_MESSAGE_SENT, METER_AI_COMPLETION, METER_CAMPAIGN_RECIPIENT)

MAX_EXTRA_SEATS = 500


def _parse_interval(raw) -> str:
    interval = str(raw or INTERVAL_MONTHLY).strip().lower()
    if interval not in BILLING_INTERVALS:
        raise APIError("billing_interval must be 'monthly' or 'yearly'", status_code=400)
    return interval

def _plan_change_blockers(org_id, plan, extra_seats) -> list[str]:
    """Reasons this org cannot move to `plan` right now (empty list = allowed)."""
    problems = []
    used_seats = seats_used(org_id)
    seat_limit = plan.included_seats + extra_seats
    if used_seats > seat_limit:
        problems.append(f"{used_seats} active agents exceed the {seat_limit} seats allowed")
    wa_used = whatsapp_accounts_used(org_id)
    if wa_used > plan.included_wa_accounts:
        problems.append(f"{wa_used} WhatsApp accounts exceed the {plan.included_wa_accounts} allowed")
    return problems

def _parse_extra_seats(raw, default: int = 0) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise APIError("extra_seats must be a whole number", status_code=400)
    if value < 0 or value > MAX_EXTRA_SEATS:
        raise APIError(f"extra_seats must be between 0 and {MAX_EXTRA_SEATS}", status_code=400)
    return value

def _org(request):
    org_id = request_organization_id(request)
    if not org_id:
        raise APIError("Organization required", status_code=400)
    return org_id


def _plan_payload(plan: BillingPlan):
    return {
        "key": plan.key,
        "name": plan.name,
        "description": plan.description,
        "currency": plan.currency,
        "display_order": plan.display_order,
        "prices": {
            "monthly": plan.price_monthly,
            "yearly": plan.price_yearly,
        },
        "extra_seat_prices": {
            "monthly": plan.extra_seat_price_monthly,
            "yearly": plan.extra_seat_price_yearly,
        },
        "included_seats": plan.included_seats,
        "included_wa_accounts": plan.included_wa_accounts,
        "features": plan.features,
        "included_quotas": plan.included_quotas,
        "is_paid": plan.is_paid,
        "has_product": bool(
            plan.dodo_product_id or plan.dodo_price_id_monthly or plan.dodo_price_id_yearly
        ),
        "checkout_ready": {
            "monthly": plan.is_checkout_ready_for_interval(INTERVAL_MONTHLY),
            "yearly": plan.is_checkout_ready_for_interval(INTERVAL_YEARLY),
        },
    }


def _sub_payload(sub: OrganizationSubscription, org_id):
    used = seats_used(org_id)
    return {
        "plan": _plan_payload(sub.plan),
        "status": sub.status,
        "is_usable": sub.is_usable,
        "billing_interval": sub.billing_interval,
        "extra_seats": sub.extra_seats,
        "seat_limit": sub.seat_limit,
        "seats_used": used,
        "seats_available": max(sub.seat_limit - used, 0),
        "cancel_at_period_end": sub.cancel_at_period_end,
        "cancelled_at": sub.cancelled_at.isoformat() if sub.cancelled_at else None,
        "pending_plan": sub.pending_plan.key if sub.pending_plan else None,
        "pending_billing_interval": sub.pending_billing_interval,
        "dodo_customer_id": sub.dodo_customer_id,
        "dodo_subscription_id": sub.dodo_subscription_id,
        "current_period_start": sub.current_period_start.isoformat() if sub.current_period_start else None,
        "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
        "dodo_configured": dodo.is_configured(),
    }


@api_view(["GET"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def list_plans(request):
    plans = BillingPlan.objects.filter(is_active=True)
    return success({"plans": [_plan_payload(p) for p in plans]})


@api_view(["GET"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def get_subscription(request):
    org_id = _org(request)
    sub = get_or_create_subscription(org_id)
    return success(_sub_payload(sub, org_id))


@api_view(["POST"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def create_checkout(request):
    org_id = _org(request)
    plan_key = (request.data.get("plan_key") or "").strip()
    interval = _parse_interval(request.data.get("billing_interval"))
    extra_seats = _parse_extra_seats(request.data.get("extra_seats"))
    
    if plan_key == PLAN_FREE:
        return error("Free plan does not require checkout")

    plan = BillingPlan.objects.filter(key=plan_key, is_active=True).first()
    if not plan or not plan.is_paid:
        return error("Unknown plan", http_status=400)
    if plan.price_for_interval(interval) <= 0:
        return error(f"{plan.name} {interval} billing is not available yet", http_status=400)

    sub = get_or_create_subscription(org_id)
    if sub.dodo_subscription_id and sub.status in (STATUS_ACTIVE, STATUS_ON_HOLD):
        return error(
            "This organization already has a subscription. Use change-plan instead.",
            http_status=409,
        )

    user = request.user
    profile = PaymentProfile.objects.filter(organization_id=org_id).first()
    customer_email = (profile.billing_email if profile else "") or user.email
    customer_name = (profile.billing_name if profile else "") or user.full_name
    try:
        product_id = dodo.ensure_plan_product(plan, interval)
        seat_addon_id = dodo.ensure_seat_addon(plan, interval) if extra_seats else ""
        session = dodo.create_checkout_session(
            product_id=product_id,
            extra_seats=extra_seats,
            seat_addon_id=seat_addon_id,
            customer_email=customer_email,
            customer_name=customer_name,
            organization_id=str(org_id),
            customer_id=sub.dodo_customer_id,
            return_url=f"{settings.DODO_RETURN_URL}?status=checkout",
            metadata={
                "plan_key": plan.key,
                "billing_interval": interval,
                "extra_seats": str(extra_seats),
            },
        )
    except DodoNotConfigured as exc:
        return error(str(exc), http_status=503)
    except Exception:
        logger.exception("Dodo checkout failed")
        return error("Failed to create checkout session", http_status=502)

    return success(session)


@api_view(["POST"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def change_plan(request):
    org_id = _org(request)
    sub = get_or_create_subscription(org_id)
    plan_key = str(request.data.get("plan_key") or "").strip()
    interval = _parse_interval(request.data.get("billing_interval") or sub.billing_interval)
    extra_seats = _parse_extra_seats(request.data.get("extra_seats"), default=sub.extra_seats)
    
    plan = BillingPlan.objects.filter(key=plan_key, is_active=True).first()
    if not plan:
        return error("Unknown plan", http_status=400)
    if plan.key == PLAN_FREE:
        return error("To move back to Free, cancel the subscription instead", http_status=400)
    if not sub.dodo_subscription_id or sub.status != STATUS_ACTIVE:
        return error("No active subscription. Start checkout first.", http_status=409)
    if sub.cancel_at_period_end:
        return error("This subscription is scheduled to cancel and cannot be changed.", http_status=409)
    if plan.price_for_interval(interval) <= 0:
        return error(f"{plan.name} {interval} billing is not available yet", http_status=400)
    problems = _plan_change_blockers(org_id, plan, extra_seats)
    if problems:
        return error(f"Cannot switch to {plan.name}: " + "; ".join(problems), http_status=409)

    try:
        product_id = dodo.ensure_plan_product(plan, interval)
        seat_addon_id = dodo.ensure_seat_addon(plan, interval) if extra_seats else ""
        dodo.change_plan(
            subscription_id=sub.dodo_subscription_id,
            product_id=product_id,
            extra_seats=extra_seats,
            seat_addon_id=seat_addon_id,
            proration_billing_mode="prorated_immediately",
        )
    except DodoNotConfigured as exc:
        return error(str(exc), http_status=503)
    except Exception:
        logger.exception("Dodo change_plan failed")
        return error("Failed to change plan", http_status=502)

    sub.pending_plan = plan
    sub.pending_billing_interval = interval
    sub.save(update_fields=["pending_plan", "pending_billing_interval", "updated_at"])

    payload = _sub_payload(sub, org_id)
    payload["change_requested"] = {
        "plan": plan.key,
        "billing_interval": interval,
        "extra_seats": extra_seats,
    }
    return success(payload)
@api_view(["POST"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def cancel_subscription(request):
    org_id = _org(request)
    sub = get_or_create_subscription(org_id)
    
    if not sub.dodo_subscription_id:
        return error("No active Dodo subscription to cancel", http_status=400)
    if sub.status == STATUS_CANCELLED or sub.cancel_at_period_end:
        return error("Subscription is already cancelled ", http_status=400)

    try:
        dodo.cancel_subscription(sub.dodo_subscription_id)
    except DodoNotConfigured as exc:
        return error(str(exc), http_status=503)
    except Exception:
        logger.exception("Dodo cancel failed")
        return error("Failed to cancel subscription", http_status=502)

    sub.cancel_at_period_end = True
    sub.cancelled_at = dj_tz.now()
    sub.save(update_fields=["cancel_at_period_end", "cancelled_at", "updated_at"])
    return success(_sub_payload(sub, org_id))

@api_view(["POST"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def update_seats(request):
    org_id = _org(request)
    sub = get_or_create_subscription(org_id)
    extra_seats = _parse_extra_seats(request.data.get("extra_seats"))

    if not sub.dodo_subscription_id or not sub.plan.is_paid:
        return error("Seat add-ons require an active paid subscription")
    if sub.status != STATUS_ACTIVE or sub.cancel_at_period_end:
        return error("Seats can only be changed on an active, non-cancelling subscription", http_status=409)
    used = seats_used(org_id)
    if used > sub.plan.included_seats + extra_seats:
        return error(
            f"Cannot reduce seats below active agents ({used})",
            http_status=409,
        )

    try:
        product_id = dodo.ensure_plan_product(sub.plan, sub.billing_interval)
        seat_addon_id = dodo.ensure_seat_addon(sub.plan, sub.billing_interval) if extra_seats else ""
        dodo.change_plan(
            subscription_id=sub.dodo_subscription_id,
            product_id=product_id,
            extra_seats=extra_seats,
            seat_addon_id=seat_addon_id,
        )
    except DodoNotConfigured as exc:
        return error(str(exc), http_status=503)
    except Exception:
        logger.exception("Dodo seat change failed")
        return error("Failed to update seats", http_status=502)

    payload = _sub_payload(sub, org_id)
    payload["change_requested"] = {"extra_seats": extra_seats}
    return success(payload)
    


@api_view(["POST"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def customer_portal(request):
    org_id = _org(request)
    sub = get_or_create_subscription(org_id)
    if not sub.dodo_customer_id:
        return error("No Dodo customer on this organization yet")
    try:
        url = dodo.customer_portal_url(sub.dodo_customer_id, return_url=settings.DODO_RETURN_URL)
    except DodoNotConfigured as extra:
        return error(str(extra), http_status=503)
    except Exception:
        logger.exception("Dodo portal failed")
        return error("Failed to open customer portal", http_status=502)
    return success({"portal_url": url})


@api_view(["GET"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def usage(request):
    org_id = _org(request)
    sub = get_or_create_subscription(org_id)
    hard_cap = bool((sub.plan.features or {}).get("hard_cap_usage"))
    meters = []
    for meter in METERS:
        used = current_usage(org_id, meter)
        included = quota_for(sub, meter)
        meters.append(
            {
                "meter": meter,
                "used": used,
                "included": included,
                "remaining": None if not (sub.plan.features or {}).get("hard_cap_usage") else max(included - used, 0),
                "overage": max(used - included, 0),
                "hard_cap": hard_cap,
            }
        )

    seats_now = seats_used(org_id)
    wa_now = whatsapp_accounts_used(org_id)
    return success(
        {
            "period_start": period_start().isoformat(),
            "seats": {
                "used": seats_now,
                "included": sub.plan.included_seats,
                "extra": sub.extra_seats,
                "limit": sub.seat_limit,
            },
            "whatsapp_accounts": {
                "used": wa_now,
                "limit": sub.plan.included_wa_accounts,
            },
            "meters": meters,
        }
    )


@api_view(["GET"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def invoices(request):
    start_date, end_date, date_error = parse_optional_date_range(
        request.query_params.get("start_date") or "",
        request.query_params.get("end_date") or "",
    )
    if date_error:
        return error(date_error, http_status=400)

    page, limit, offset = parse_pagination(request)
    org_id = _org(request)
    sub = get_or_create_subscription(org_id)
    if not sub.dodo_customer_id:
        return success(list_payload("invoices", [], 0, page, limit))
    try:
        # Fetch every provider page so filtered totals and local pagination
        # describe the customer's complete invoice history.
        items = dodo.list_payments(sub.dodo_customer_id, limit=None)
    except DodoNotConfigured:
        return success(list_payload("invoices", [], 0, page, limit))
    except Exception:
        logger.exception("Dodo invoices failed")
        return error("Failed to load invoices", http_status=502)
    filtered_items = []
    for item in items:
        invoice_date = item.get("created_at")
        if start_date or end_date:
            try:
                if isinstance(invoice_date, datetime):
                    parsed_date = invoice_date
                else:
                    parsed_date = datetime.fromisoformat(str(invoice_date).replace("Z", "+00:00"))
                if parsed_date.tzinfo is None:
                    parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                parsed_date = parsed_date.astimezone(timezone.utc)
            except (TypeError, ValueError):
                logger.warning("Skipping invoice with invalid created_at: %r", invoice_date)
                continue
            if start_date and parsed_date < start_date:
                continue
            if end_date and parsed_date > end_date:
                continue
        filtered_items.append(item)

    for item in filtered_items:
        # invoice_pdf only serves successful payments (see its 400 guard below),
        # so don't hand the frontend a link that will 400 if it's clicked.
        item["invoice_url"] = (
            reverse("billing-invoice-pdf", args=[item["id"]])
            if item.get("status") == "succeeded"
            else None
        )

    return success(
        list_payload(
            "invoices",
            filtered_items[offset : offset + limit],
            len(filtered_items),
            page,
            limit,
        )
    )

@api_view(["GET"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def invoice_pdf(request, payment_id):
    """
    Generate and return a PDF invoice for a successful Dodo payment.
    The payment must belong to the authenticated organization's
    Dodo customer.
    """
    org_id = _org(request)
    sub = get_or_create_subscription(org_id)

    if not sub.dodo_customer_id:
        return error(
            "No Dodo customer on this organization",
            http_status=404,
        )

    try:
        details = dodo.get_payment_details(payment_id)
    except DodoNotConfigured as exc:
        return error(str(exc), http_status=503)
    except Exception:
        logger.exception("Dodo payment retrieval failed")
        return error(
            "Failed to retrieve payment",
            http_status=502,
        )

    payment = details["payment"]

    # Prevent cross-organization access.
    payment_customer = getattr(payment, "customer", None)
    payment_customer_id = getattr(payment_customer, "customer_id", "")

    if payment_customer_id != sub.dodo_customer_id:
        return error(
            "Payment does not belong to this organization",
            http_status=403,
        )

    if payment.status != "succeeded":
        return error(
            "Invoice is only available for successful payments",
            http_status=400,
        )

    # Get the existing Payment Profile for this organization.
    payment_profile = PaymentProfile.objects.filter(
        organization_id=org_id
    ).first()

    try:
        invoice_data = build_invoice_data(
            payment,
            details["line_items"],
            payment_profile=payment_profile,
        )

        pdf_bytes = generate_invoice_pdf(invoice_data)
    except ValueError as exc:
        return error(
            str(exc),
            http_status=400,
        )
    except Exception:
        logger.exception("Invoice PDF generation failed")
        return error(
            "Failed to generate invoice PDF",
            http_status=500,
        )

    response = HttpResponse(
        pdf_bytes,
        content_type="application/pdf",
    )

    invoice_number = (
        invoice_data.get("invoice_id")
        or payment.payment_id
    )

    response["Content-Disposition"] = (
        f'attachment; filename="invoice-{invoice_number}.pdf"'
    )

    return response

@api_view(["POST"])
@permission_classes([CookieAuthenticated, HasOrg, HasBillingAccess])
def create_payment_profile(request):
    """
    Create a payment profile for the authenticated user's organization.
    Each organization can have only one payment profile.
    """
    org_id = _org(request)

    if PaymentProfile.objects.filter(organization_id=org_id).exists():
        return error(
            "Payment profile already exists",
            http_status=400,
        )

    serializer = PaymentProfileSerializer(data=request.data)

    if not serializer.is_valid():
        return error(
            "Validation failed",
            http_status=400,
            extra=serializer.errors,
        )

    payment_profile = serializer.save(
        organization_id=org_id
    )

    return success(
        PaymentProfileSerializer(payment_profile).data,
        http_status=201,
    )


@api_view(["GET"])
@permission_classes([CookieAuthenticated, HasOrg, HasBillingAccess])
def get_payment_profile(request):
    """
    Retrieve the payment profile belonging to the authenticated user's organization.
    """
    org_id = _org(request)

    try:
        payment_profile = PaymentProfile.objects.get(
            organization_id=org_id
        )
    except PaymentProfile.DoesNotExist:
        return error(
            "Payment profile not found",
            http_status=404,
        )

    return success(
        PaymentProfileSerializer(payment_profile).data
    )


@api_view(["PUT", "PATCH"])
@permission_classes([CookieAuthenticated, HasOrg, HasBillingAccess])
def update_payment_profile(request):
    """
    Update the payment profile belonging to the authenticated user's organization.
    """
    org_id = _org(request)

    try:
        payment_profile = PaymentProfile.objects.get(
            organization_id=org_id
        )
    except PaymentProfile.DoesNotExist:
        return error(
            "Payment profile not found",
            http_status=404,
        )

    serializer = PaymentProfileSerializer(
        payment_profile,
        data=request.data,
        partial=request.method == "PATCH",
    )

    if not serializer.is_valid():
        return error(
            "Validation failed",
            http_status=400,
            extra=serializer.errors,
        )

    payment_profile = serializer.save()

    return success(
        PaymentProfileSerializer(payment_profile).data
    )
