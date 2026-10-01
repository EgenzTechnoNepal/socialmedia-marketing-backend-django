from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from apps.billing.models import BillingPlan
from apps.billing.services import dodo
from apps.billing.webhooks import _extra_seats


@override_settings(DODO_ADDON_SEAT="")
class DodoCatalogTests(TestCase):
    def setUp(self):
        self.plan = BillingPlan.objects.create(
            key="pro",
            name="Pro",
            description="Pro plan",
            currency="USD",
            price_monthly=2900,
            price_yearly=29000,
            extra_seat_price_monthly=500,
            extra_seat_price_yearly=5000,
        )
        self.client = Mock()
        self.client.products.create.return_value = SimpleNamespace(product_id="pdt_pro_monthly")
        self.client.addons.create.return_value = SimpleNamespace(addon_id="addon_pro_monthly")

    @patch("apps.billing.services.dodo._client")
    def test_plan_product_is_created_once_and_uses_sku_metadata(self, make_client):
        make_client.return_value = self.client

        first_id = dodo.ensure_plan_product(self.plan, "monthly")
        second_id = dodo.ensure_plan_product(self.plan, "monthly")

        self.assertEqual(first_id, "pdt_pro_monthly")
        self.assertEqual(second_id, first_id)
        self.client.products.create.assert_called_once()
        kwargs = self.client.products.create.call_args.kwargs
        self.assertEqual(kwargs["metadata"]["whatomate_sku"], "whatomate:plan:pro:monthly:usd:2900")
        self.assertEqual(kwargs["extra_headers"]["Idempotency-Key"], "whatomate:plan:pro:monthly:usd:2900")
        self.assertEqual(kwargs["price"]["payment_frequency_interval"], "Month")

    @patch("apps.billing.services.dodo._client")
    def test_price_change_creates_new_product_and_archives_old_id(self, make_client):
        make_client.return_value = self.client
        self.client.products.create.side_effect = [
            SimpleNamespace(product_id="pdt_pro_monthly_v1"),
            SimpleNamespace(product_id="pdt_pro_monthly_v2"),
        ]

        self.assertEqual(dodo.ensure_plan_product(self.plan, "monthly"), "pdt_pro_monthly_v1")
        self.plan.refresh_from_db()
        self.plan.price_monthly = 3100
        self.plan.save(update_fields=["price_monthly"])

        self.assertEqual(dodo.ensure_plan_product(self.plan, "monthly"), "pdt_pro_monthly_v2")
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.dodo_price_id_monthly, "pdt_pro_monthly_v2")
        self.assertEqual(self.plan.dodo_former_product_ids, ["pdt_pro_monthly_v1"])
        self.assertEqual(
            self.client.products.create.call_args.kwargs["metadata"]["whatomate_sku"],
            "whatomate:plan:pro:monthly:usd:3100",
        )

    @patch("apps.billing.services.dodo._client")
    def test_seat_addon_is_created_once(self, make_client):
        make_client.return_value = self.client

        first_id = dodo.ensure_seat_addon(self.plan, "monthly")
        second_id = dodo.ensure_seat_addon(self.plan, "monthly")

        self.assertEqual(first_id, "addon_pro_monthly")
        self.assertEqual(second_id, first_id)
        self.client.addons.create.assert_called_once()
        self.assertEqual(self.client.addons.create.call_args.kwargs["price"], 500)
        self.assertEqual(
            self.client.addons.create.call_args.kwargs["description"],
            "One extra seat for Pro monthly; catalog key whatomate:seat:pro:monthly:usd:500",
        )
        self.assertEqual(
            self.client.addons.create.call_args.kwargs["extra_headers"]["Idempotency-Key"],
            "whatomate:seat:pro:monthly:usd:500",
        )

    def test_webhook_recognizes_interval_specific_seat_addons(self):
        self.plan.dodo_seat_addon_id_monthly = "addon_pro_monthly"
        self.plan.dodo_seat_addon_id_yearly = "addon_pro_yearly"
        self.plan.save(update_fields=["dodo_seat_addon_id_monthly", "dodo_seat_addon_id_yearly"])

        for addon_id in ("addon_pro_monthly", "addon_pro_yearly"):
            with self.subTest(addon_id=addon_id):
                self.assertEqual(
                    _extra_seats({"addons": [{"addon_id": addon_id, "quantity": 3}]}, self.plan),
                    3,
                )
