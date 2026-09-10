from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes

from .serializers import PaymentProfileSerializer

from apps.common.envelope import error, success
from apps.common.exceptions import APIError
from apps.common.permissions import (
    CookieAuthenticated,
    HasOrg,
    HasBillingAccess,
    request_organization_id,
)
from .entitlements import (
    METER_AI_COMPLETION,
    METER_CAMPAIGN_RECIPIENT,
    METER_MESSAGE_SENT,
    current_usage,
    get_or_create_subscription,
    quota_for,
    seats_used,
)
from .models import (
    PLAN_FREE,
    STATUS_ACTIVE,
    BillingPlan,
    OrganizationSubscription,
    PaymentProfile,
)
from .services import dodo
from .services.dodo import DodoNotConfigured
from .services.invoice import build_invoice_data, generate_invoice_pdf

logger = logging.getLogger(__name__)

METERS = (METER_MESSAGE_SENT, METER_AI_COMPLETION, METER_CAMPAIGN_RECIPIENT)


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
        "included_seats": plan.included_seats,
        "features": plan.features,
        "included_quotas": plan.included_quotas,
        "is_paid": plan.is_paid,
        "has_product": bool(plan.dodo_product_id),
    }


def _sub_payload(sub: OrganizationSubscription, org_id):
    used = seats_used(org_id)
    return {
        "plan": _plan_payload(sub.plan),
        "status": sub.status,
        "extra_seats": sub.extra_seats,
        "seat_limit": sub.seat_limit,
        "seats_used": used,
        "seats_available": max(sub.seat_limit - used, 0),
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
    extra_seats = int(request.data.get("extra_seats") or 0)
    if extra_seats < 0:
        return error("extra_seats must be >= 0")
    if plan_key == PLAN_FREE:
        return error("Free plan does not require checkout")

    plan = BillingPlan.objects.filter(key=plan_key, is_active=True).first()
    if not plan or not plan.dodo_product_id:
        return error("Unknown or unconfigured plan", http_status=400)

    user = request.user
    try:
        session = dodo.create_checkout_session(
            product_id=plan.dodo_product_id,
            extra_seats=extra_seats,
            seat_addon_id=plan.dodo_seat_addon_id or settings.DODO_ADDON_SEAT,
            customer_email=user.email,
            customer_name=user.full_name,
            organization_id=str(org_id),
            customer_id=get_or_create_subscription(org_id).dodo_customer_id,
            return_url=f"{settings.DODO_RETURN_URL}?status=checkout",
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
    plan_key = (request.data.get("plan_key") or "").strip()
    extra_seats = request.data.get("extra_seats")
    if extra_seats is None:
        extra_seats = sub.extra_seats
    extra_seats = int(extra_seats)

    plan = BillingPlan.objects.filter(key=plan_key, is_active=True).first()
    if not plan:
        return error("Unknown plan")
    if plan.key == PLAN_FREE:
        return error("Use Dodo customer portal or cancel to return to Free")
    if not sub.dodo_subscription_id:
        return error("No active Dodo subscription. Start checkout first.")
    if extra_seats < 0:
        return error("extra_seats must be >= 0")
    used = seats_used(org_id)
    if used > plan.included_seats + extra_seats:
        return error(f"Cannot reduce seats below active agents ({used})")

    try:
        dodo.change_plan(
            subscription_id=sub.dodo_subscription_id,
            product_id=plan.dodo_product_id,
            extra_seats=extra_seats,
            seat_addon_id=plan.dodo_seat_addon_id or settings.DODO_ADDON_SEAT,
        )
    except DodoNotConfigured as exc:
        return error(str(exc), http_status=503)
    except Exception:
        logger.exception("Dodo change_plan failed")
        return error("Failed to change plan", http_status=502)

    sub.plan = plan
    sub.extra_seats = extra_seats
    sub.status = STATUS_ACTIVE
    sub.save(update_fields=["plan", "extra_seats", "status", "updated_at"])
    return success(_sub_payload(sub, org_id))


@api_view(["POST"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def update_seats(request):
    org_id = _org(request)
    sub = get_or_create_subscription(org_id)
    extra_seats = int(request.data.get("extra_seats") or 0)
    if extra_seats < 0:
        return error("extra_seats must be >= 0")
    if not sub.dodo_subscription_id or not sub.plan.is_paid:
        return error("Seat add-ons require an active paid subscription")
    used = seats_used(org_id)
    if used > sub.plan.included_seats + extra_seats:
        return error(f"Cannot reduce seats below active agents ({used})")

    try:
        dodo.change_plan(
            subscription_id=sub.dodo_subscription_id,
            product_id=sub.plan.dodo_product_id,
            extra_seats=extra_seats,
            seat_addon_id=sub.plan.dodo_seat_addon_id or settings.DODO_ADDON_SEAT,
        )
    except DodoNotConfigured as extra:
        return error(str(extra), http_status=503)
    except Exception:
        logger.exception("Dodo seat change failed")
        return error("Failed to update seats", http_status=502)

    sub.extra_seats = extra_seats
    sub.save(update_fields=["extra_seats", "updated_at"])
    return success(_sub_payload(sub, org_id))


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
            }
        )
    return success(
        {
            "period_start": sub.current_period_start.isoformat()
            if sub.current_period_start
            else None,
            "meters": meters,
        }
    )


@api_view(["GET"])
@permission_classes([CookieAuthenticated, HasBillingAccess])
def invoices(request):
    org_id = _org(request)
    sub = get_or_create_subscription(org_id)
    if not sub.dodo_customer_id:
        return success({"invoices": []})
    try:
        items = dodo.list_payments(sub.dodo_customer_id)
    except DodoNotConfigured:
        return success({"invoices": []})
    except Exception:
        logger.exception("Dodo invoices failed")
        return error("Failed to load invoices", http_status=502)
    return success({"invoices": items})

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