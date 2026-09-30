from django.db import transaction

from apps.billing.models import (
    BILLING_INTERVALS,
    DodoProductSync,
    BillingPlan,
    PLAN_FREE,
)
from apps.billing.services import dodo


def _sync_key(plan: BillingPlan, interval: str) -> str:
    amount = plan.price_for_interval(interval)
    currency = (plan.currency or "USD").lower()

    return f"whatomate:plan:{plan.key}:{interval}:{currency}:{amount}"


def ensure_synced_plan_product(plan_id, interval: str) -> str:
    """
    Ensure a paid plan interval has a Dodo product.

    The BillingPlan row is locked so concurrent requests for the
    same plan cannot create duplicate Dodo products.
    """
    if interval not in BILLING_INTERVALS:
        raise ValueError(f"Unknown billing interval: {interval}")

    try:
        with transaction.atomic():
            plan = (
                BillingPlan.objects
                .select_for_update()
                .get(pk=plan_id)
            )

            if plan.key == PLAN_FREE or not plan.is_paid:
                raise ValueError("Free plans do not have Dodo products")

            amount = plan.price_for_interval(interval)
            currency = (plan.currency or "USD").upper()
            sync_key = _sync_key(plan, interval)

            sync_record, _ = DodoProductSync.objects.get_or_create(
                plan=plan,
                interval=interval,
                currency=currency,
                amount=amount,
                defaults={
                    "sync_key": sync_key,
                    "sync_status": DodoProductSync.STATUS_PENDING,
                },
            )

            if (
                sync_record.sync_status == DodoProductSync.STATUS_SYNCED
                and sync_record.dodo_product_id
            ):
                return sync_record.dodo_product_id

            sync_record.sync_key = sync_key
            sync_record.sync_status = DodoProductSync.STATUS_PENDING
            sync_record.sync_error = ""
            sync_record.save(
                update_fields=[
                    "sync_key",
                    "sync_status",
                    "sync_error",
                    "updated_at",
                ]
            )

            product_id = dodo.ensure_plan_product(plan, interval)

            sync_record.dodo_product_id = product_id
            sync_record.sync_status = DodoProductSync.STATUS_SYNCED
            sync_record.sync_error = ""
            sync_record.save(
                update_fields=[
                    "dodo_product_id",
                    "sync_status",
                    "sync_error",
                    "updated_at",
                ]
            )

            return product_id

    except Exception as exc:
        try:
            plan = BillingPlan.objects.get(pk=plan_id)

            # Free plans are intentionally excluded from Dodo product sync.
            if plan.key != PLAN_FREE and plan.is_paid:
                amount = plan.price_for_interval(interval)
                currency = (plan.currency or "USD").upper()
                sync_key = _sync_key(plan, interval)

                DodoProductSync.objects.update_or_create(
                    plan=plan,
                    interval=interval,
                    currency=currency,
                    amount=amount,
                    defaults={
                        "sync_key": sync_key,
                        "sync_status": DodoProductSync.STATUS_ERROR,
                        "sync_error": str(exc),
                    },
                )
        except Exception:
        # Preserve the original sync exception.
            pass

        raise