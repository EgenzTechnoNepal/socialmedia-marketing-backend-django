from unittest.mock import patch

from django.test import TransactionTestCase

from apps.billing.models import (
    BILLING_INTERVALS,
    BillingPlan,
    INTERVAL_MONTHLY,
    INTERVAL_YEARLY,
    PLAN_FREE,
    PLAN_PRO,
)
from apps.billing.services.dodo_sync import ensure_synced_plan_product


class DodoSyncServiceTests(TransactionTestCase):
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

    @patch("apps.billing.services.dodo_sync.dodo.ensure_plan_product")
    def test_paid_plan_delegates_to_dodo_sync(self, mock_ensure):
        mock_ensure.return_value = "dodo_prod_pro_monthly"

        product_id = ensure_synced_plan_product(
            self.plan.id,
            INTERVAL_MONTHLY,
        )

        self.assertEqual(product_id, "dodo_prod_pro_monthly")
        mock_ensure.assert_called_once_with(
            self.plan,
            INTERVAL_MONTHLY,
        )

    @patch("apps.billing.services.dodo_sync.dodo.ensure_plan_product")
    def test_yearly_plan_delegates_to_dodo_sync(self, mock_ensure):
        mock_ensure.return_value = "dodo_prod_pro_yearly"

        product_id = ensure_synced_plan_product(
            self.plan.id,
            INTERVAL_YEARLY,
        )

        self.assertEqual(product_id, "dodo_prod_pro_yearly")
        mock_ensure.assert_called_once_with(
            self.plan,
            INTERVAL_YEARLY,
        )

    def test_supported_billing_intervals_are_monthly_and_yearly(self):
        self.assertEqual(
            BILLING_INTERVALS,
            (INTERVAL_MONTHLY, INTERVAL_YEARLY),
        )

    @patch("apps.billing.services.dodo_sync.dodo.ensure_plan_product")
    def test_free_plan_is_rejected(self, mock_ensure):
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

        with self.assertRaisesMessage(
            ValueError,
            "Free plans do not have Dodo products",
        ):
            ensure_synced_plan_product(
                free_plan.id,
                INTERVAL_MONTHLY,
            )

        mock_ensure.assert_not_called()

    @patch("apps.billing.services.dodo_sync.dodo.ensure_plan_product")
    def test_invalid_interval_is_rejected(self, mock_ensure):
        with self.assertRaisesMessage(
            ValueError,
            "Unknown billing interval: weekly",
        ):
            ensure_synced_plan_product(
                self.plan.id,
                "weekly",
            )

        mock_ensure.assert_not_called()

    @patch("apps.billing.services.dodo_sync.dodo.ensure_plan_product")
    def test_dodo_error_is_propagated(self, mock_ensure):
        mock_ensure.side_effect = RuntimeError("Dodo API unavailable")

        with self.assertRaisesMessage(
            RuntimeError,
            "Dodo API unavailable",
        ):
            ensure_synced_plan_product(
                self.plan.id,
                INTERVAL_MONTHLY,
            )

        mock_ensure.assert_called_once_with(
            self.plan,
            INTERVAL_MONTHLY,
        )

    @patch("apps.billing.services.dodo_sync.dodo.ensure_plan_product")
    def test_sync_uses_plan_id_and_passes_locked_plan(self, mock_ensure):
        mock_ensure.return_value = "dodo_prod_pro_monthly"

        product_id = ensure_synced_plan_product(
            self.plan.id,
            INTERVAL_MONTHLY,
        )

        self.assertEqual(product_id, "dodo_prod_pro_monthly")

        locked_plan = mock_ensure.call_args.args[0]

        self.assertEqual(locked_plan.id, self.plan.id)
        self.assertEqual(locked_plan.key, PLAN_PRO)
        self.assertEqual(locked_plan.price_monthly, 2900)
        self.assertEqual(locked_plan.currency, "USD")