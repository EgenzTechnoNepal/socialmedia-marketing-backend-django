import threading
from unittest.mock import patch

from django.db import close_old_connections

from django.test import TransactionTestCase

from apps.billing.models import (
    BillingPlan,
    DodoProductSync,
    INTERVAL_MONTHLY,
    PLAN_FREE,
    PLAN_PRO,
)
from apps.billing.services.dodo_sync import ensure_synced_plan_product


class DodoProductSyncTests(TransactionTestCase):
    def setUp(self):
        self.plan = BillingPlan.objects.create(
            key=PLAN_PRO,
            name="Pro",
            description="Pro plan",
            currency="USD",
            price_monthly=2900,
            price_yearly=29000,
            included_seats=5,
            included_wa_accounts=2,
            is_active=True,
        )

    @patch(
        "apps.billing.services.dodo_sync.dodo.ensure_plan_product",
        create=True,
    )
    def test_paid_plan_product_is_synced(self, mock_ensure):
        mock_ensure.return_value = "dodo_prod_pro_monthly"

        product_id = ensure_synced_plan_product(
            self.plan.id,
            INTERVAL_MONTHLY,
        )

        self.assertEqual(product_id, "dodo_prod_pro_monthly")
        mock_ensure.assert_called_once()

        sync_record = DodoProductSync.objects.get(
            plan=self.plan,
            interval=INTERVAL_MONTHLY,
        )

        self.assertEqual(
            sync_record.sync_key,
            "whatomate:plan:pro:monthly:usd:2900",
        )
        self.assertEqual(
            sync_record.dodo_product_id,
            "dodo_prod_pro_monthly",
        )
        self.assertEqual(
            sync_record.sync_status,
            DodoProductSync.STATUS_SYNCED,
        )
        self.assertEqual(sync_record.sync_error, "")

    @patch(
        "apps.billing.services.dodo_sync.dodo.ensure_plan_product",
        create=True,
    )
    def test_second_sync_does_not_call_dodo_again(self, mock_ensure):
        mock_ensure.return_value = "dodo_prod_pro_monthly"

        first_result = ensure_synced_plan_product(
            self.plan.id,
            INTERVAL_MONTHLY,
        )
        second_result = ensure_synced_plan_product(
            self.plan.id,
            INTERVAL_MONTHLY,
        )

        self.assertEqual(first_result, "dodo_prod_pro_monthly")
        self.assertEqual(second_result, "dodo_prod_pro_monthly")
        mock_ensure.assert_called_once()

        self.assertEqual(
            DodoProductSync.objects.filter(
                plan=self.plan,
                interval=INTERVAL_MONTHLY,
            ).count(),
            1,
        )

    @patch(
        "apps.billing.services.dodo_sync.dodo.ensure_plan_product",
        create=True,
    )
    def test_free_plan_never_creates_dodo_product(self, mock_ensure):
        free_plan = BillingPlan.objects.create(
            key=PLAN_FREE,
            name="Free",
            description="Free plan",
            currency="USD",
            price_monthly=0,
            price_yearly=0,
            included_seats=1,
            included_wa_accounts=1,
            is_active=True,
        )

        with self.assertRaises(ValueError):
            ensure_synced_plan_product(
                free_plan.id,
                INTERVAL_MONTHLY,
            )

        mock_ensure.assert_not_called()

        self.assertFalse(
            DodoProductSync.objects.filter(
                plan=free_plan,
            ).exists()
        )

    @patch(
        "apps.billing.services.dodo_sync.dodo.ensure_plan_product",
        create=True,
    )
    def test_dodo_failure_is_saved_as_error(self, mock_ensure):
        mock_ensure.side_effect = RuntimeError("Dodo API unavailable")

        with self.assertRaises(RuntimeError):
            ensure_synced_plan_product(
                self.plan.id,
                INTERVAL_MONTHLY,
            )

        sync_record = DodoProductSync.objects.get(
            plan=self.plan,
            interval=INTERVAL_MONTHLY,
        )

        self.assertEqual(
            sync_record.sync_status,
            DodoProductSync.STATUS_ERROR,
        )
        self.assertEqual(
            sync_record.sync_error,
            "Dodo API unavailable",
        )
        self.assertEqual(sync_record.dodo_product_id, "")


    @patch(
        "apps.billing.services.dodo_sync.dodo.ensure_plan_product",
        create=True,
    )
    def test_concurrent_sync_is_serialized_by_plan_lock(self, mock_ensure):
        plan_id = self.plan.id

        # Make sure the row is committed and visible to other
        # database connections before starting worker threads.
        self.plan.save()

        mock_ensure.return_value = "dodo_prod_pro_monthly"

        results = []
        errors = []
        results_lock = threading.Lock()

        def sync_product():
            close_old_connections()

            try:
                result = ensure_synced_plan_product(
                    plan_id,
                    INTERVAL_MONTHLY,
                )

                with results_lock:
                    results.append(result)

            except Exception as exc:
                with results_lock:
                    errors.append(exc)

            finally:
                close_old_connections()

        thread_one = threading.Thread(target=sync_product)
        thread_two = threading.Thread(target=sync_product)

        thread_one.start()
        thread_two.start()

        thread_one.join()
        thread_two.join()

        self.assertEqual(errors, [])

        self.assertEqual(
            sorted(results),
            [
                "dodo_prod_pro_monthly",
                "dodo_prod_pro_monthly",
            ],
        )

        mock_ensure.assert_called_once()

        self.assertEqual(
            DodoProductSync.objects.filter(
                plan_id=plan_id,
                interval=INTERVAL_MONTHLY,
            ).count(),
            1,
        )