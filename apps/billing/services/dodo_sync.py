from django.db import transaction

from apps.billing.models import BILLING_INTERVALS, BillingPlan, PLAN_FREE
from apps.billing.services import dodo


def ensure_synced_plan_product(plan_id, interval: str) -> str:
    """
    Ensure a paid plan interval has a Dodo product.

    BillingPlan is the source of truth for Dodo synchronization.
    The plan row is locked to prevent concurrent sync requests
    from creating duplicate products.
    """
    if interval not in BILLING_INTERVALS:
        raise ValueError(f"Unknown billing interval: {interval}")

    with transaction.atomic():
        plan = (
            BillingPlan.objects
            .select_for_update()
            .get(pk=plan_id)
        )

        if plan.key == PLAN_FREE or not plan.is_paid:
            raise ValueError("Free plans do not have Dodo products")

        product_id = dodo.ensure_plan_product(plan, interval)

        plan.refresh_from_db()

        return product_id