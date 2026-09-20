"""
Billing API tests (Step 17).

Put this file in: apps/billing/tests/test_billing_api.py
Run with:        python manage.py test apps.billing.tests.test_billing_api

Dodo is never called for real: every Dodo function is patched.
"""

import json
from unittest.mock import patch
from uuid import uuid4

from django.test import Client, TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Organization
from apps.billing.models import (
    PLAN_BUSINESS,
    PLAN_FREE,
    PLAN_PRO,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_FAILED,
    BillingPayment,
    BillingPlan,
    DodoWebhookEvent,
    OrganizationSubscription,
    PaymentProfile,
)

BASE = "/api/billing"
WEBHOOK_URL = f"{BASE}/webhooks/dodo"


def make_org():
    # ADJUST: add any other required fields your Organization model has.
    return Organization.objects.create(name=f"Test Org {uuid4().hex[:6]}")


class MockUser:
    is_authenticated = True
    is_super_admin = True
    email = "admin@example.com"
    full_name = "Admin User"

    def __init__(self, organization_id):
        self.organization_id = organization_id
        self.token_organization_id = organization_id
        self.is_super_admin_claim = True

    def has_permission(self, resource, action):
        return True


@override_settings(
    DODO_ADDON_SEAT="addon_seat",
    DODO_RETURN_URL="https://app.test/billing",
)
class BillingTestBase(TestCase):
    def setUp(self):
        self.org = make_org()
        self.other_org = make_org()

        self.client = APIClient()
        self.client.force_authenticate(user=MockUser(self.org.id))
        self.client.credentials(HTTP_AUTHORIZATION="Bearer test-token")

        self.free, _ = BillingPlan.objects.update_or_create(
            key=PLAN_FREE,
            defaults=dict(
                name="Free", included_seats=1, included_wa_accounts=1,
                display_order=0, price_monthly=0, price_yearly=0,
                features={"ai": False}, included_quotas={"message.sent": 100},
            ),
        )
        self.pro, _ = BillingPlan.objects.update_or_create(
            key=PLAN_PRO,
            defaults=dict(
                name="Pro", included_seats=3, included_wa_accounts=3,
                display_order=1, price_monthly=2900, price_yearly=29000,
                extra_seat_price_monthly=500, extra_seat_price_yearly=5000,
                features={"ai": True}, included_quotas={"message.sent": 5000},
                dodo_product_id="pdt_pro_m",
                dodo_price_id_monthly="pdt_pro_m",
                dodo_price_id_yearly="pdt_pro_y",
            ),
        )
        self.business, _ = BillingPlan.objects.update_or_create(
            key=PLAN_BUSINESS,
            defaults=dict(
                name="Business", included_seats=10, included_wa_accounts=10,
                display_order=2, price_monthly=9900, price_yearly=99000,
                features={"ai": True}, included_quotas={"message.sent": 50000},
                dodo_product_id="pdt_biz_m",
                dodo_price_id_monthly="pdt_biz_m",
                dodo_price_id_yearly="pdt_biz_y",
            ),
        )

    def make_paid_sub(self, plan=None, **extra):
        return OrganizationSubscription.objects.create(
            organization=self.org,
            plan=plan or self.pro,
            status=STATUS_ACTIVE,
            dodo_customer_id="cus_1",
            dodo_subscription_id="sub_1",
            **extra,
        )


class PlansApiTests(BillingTestBase):
    def test_plans_return_prices_features_and_quotas(self):
        response = self.client.get(f"{BASE}/plans")
        self.assertEqual(response.status_code, 200)
        plans = {p["key"]: p for p in response.data["data"]["plans"]}

        self.assertEqual(set(plans), {"free", "pro", "business"})
        pro = plans["pro"]
        self.assertEqual(pro["prices"], {"monthly": 2900, "yearly": 29000})
        self.assertEqual(pro["extra_seat_prices"]["monthly"], 500)
        self.assertEqual(pro["included_seats"], 3)
        self.assertEqual(pro["included_wa_accounts"], 3)
        self.assertEqual(pro["included_quotas"]["message.sent"], 5000)
        self.assertTrue(pro["checkout_ready"]["monthly"])
        self.assertFalse(plans["free"]["checkout_ready"]["monthly"])

    def test_plans_never_leak_dodo_ids(self):
        response = self.client.get(f"{BASE}/plans")
        body = json.dumps(response.data)
        for secret in ("pdt_pro_m", "pdt_pro_y", "pdt_biz_m", "pdt_biz_y"):
            self.assertNotIn(secret, body)


class SubscriptionAndUsageApiTests(BillingTestBase):
    def test_new_org_starts_on_free(self):
        response = self.client.get(f"{BASE}/subscription")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(data["plan"]["key"], "free")
        self.assertEqual(data["status"], "free")
        self.assertFalse(data["cancel_at_period_end"])

    def test_subscription_exposes_interval_and_seat_info(self):
        self.make_paid_sub(billing_interval="yearly", extra_seats=2)
        data = self.client.get(f"{BASE}/subscription").data["data"]
        self.assertEqual(data["billing_interval"], "yearly")
        self.assertEqual(data["seat_limit"], 5)  # 3 included + 2 extra

    @patch("apps.billing.views.whatsapp_accounts_used", return_value=2)
    @patch("apps.billing.views.seats_used", return_value=1)
    def test_usage_reports_seats_whatsapp_and_meters(self, _seats, _wa):
        self.make_paid_sub(extra_seats=1)
        data = self.client.get(f"{BASE}/usage").data["data"]

        self.assertEqual(data["seats"]["used"], 1)
        self.assertEqual(data["seats"]["limit"], 4)
        self.assertEqual(data["whatsapp_accounts"], {"used": 2, "limit": 3})
        meters = {m["meter"] for m in data["meters"]}
        self.assertEqual(
            meters,
            {"message.sent", "campaign.recipient", "ai.completion"},
        )


class CheckoutApiTests(BillingTestBase):
    good_session = {"session_id": "cs_1", "checkout_url": "https://pay.test/cs_1"}

    @patch("apps.billing.services.dodo.create_checkout_session")
    def test_checkout_ignores_frontend_price_product_and_org(self, mocked):
        mocked.return_value = self.good_session
        response = self.client.post(
            f"{BASE}/checkout",
            {
                "plan_key": "pro",
                "billing_interval": "yearly",
                "extra_seats": 2,
                # attacker-controlled values that must be ignored:
                "organization_id": str(self.other_org.id),
                "product_id": "pdt_evil",
                "price": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["product_id"], "pdt_pro_y")  # from DB
        self.assertEqual(kwargs["organization_id"], str(self.org.id))  # from token
        self.assertEqual(kwargs["extra_seats"], 2)
        self.assertEqual(kwargs["metadata"]["plan_key"], "pro")
        self.assertEqual(response.data["data"]["checkout_url"], "https://pay.test/cs_1")

    @patch("apps.billing.services.dodo.create_checkout_session")
    def test_checkout_rejects_bad_input_without_calling_dodo(self, mocked):
        bad_bodies = [
            {"plan_key": "free"},
            {"plan_key": "nope"},
            {"plan_key": ""},
            {"plan_key": "pro", "billing_interval": "weekly"},
            {"plan_key": "pro", "extra_seats": "abc"},
            {"plan_key": "pro", "extra_seats": -1},
            {"plan_key": "pro", "extra_seats": 1000000},
        ]
        for body in bad_bodies:
            with self.subTest(body=body):
                response = self.client.post(f"{BASE}/checkout", body, format="json")
                self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()

    @patch("apps.billing.services.dodo.create_checkout_session")
    def test_checkout_blocked_when_interval_not_configured(self, mocked):
        self.pro.dodo_price_id_yearly = ""
        self.pro.save()
        response = self.client.post(
            f"{BASE}/checkout",
            {"plan_key": "pro", "billing_interval": "yearly"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()

    @patch("apps.billing.services.dodo.create_checkout_session")
    def test_checkout_blocked_when_already_subscribed(self, mocked):
        self.make_paid_sub()
        response = self.client.post(f"{BASE}/checkout", {"plan_key": "business"}, format="json")
        self.assertEqual(response.status_code, 409)
        mocked.assert_not_called()


@patch("apps.billing.views.whatsapp_accounts_used", return_value=0)
@patch("apps.billing.views.seats_used", return_value=1)
class ChangePlanApiTests(BillingTestBase):
    @patch("apps.billing.services.dodo.change_plan")
    def test_upgrade_calls_dodo_and_records_pending_change(self, mocked, _seats, _wa):
        sub = self.make_paid_sub(plan=self.pro)
        response = self.client.post(
            f"{BASE}/change-plan",
            {"plan_key": "business", "billing_interval": "monthly"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["product_id"], "pdt_biz_m")
        self.assertEqual(kwargs["proration_billing_mode"], "prorated_immediately")
        self.assertEqual(response.data["data"]["change_requested"]["plan"], "business")

        sub.refresh_from_db()
        self.assertEqual(sub.plan_id, self.pro.id)  # webhook applies the confirmed plan
        self.assertEqual(sub.pending_plan_id, self.business.id)
        self.assertEqual(sub.pending_billing_interval, "monthly")

    @patch("apps.billing.services.dodo.change_plan")
    def test_downgrade_blocked_by_seats(self, mocked, seats, _wa):
        seats.return_value = 5  # Pro allows 3
        self.make_paid_sub(plan=self.business)
        response = self.client.post(f"{BASE}/change-plan", {"plan_key": "pro"}, format="json")
        self.assertEqual(response.status_code, 409)
        mocked.assert_not_called()

    @patch("apps.billing.services.dodo.change_plan")
    def test_downgrade_blocked_by_whatsapp_accounts(self, mocked, _seats, wa):
        wa.return_value = 5  # Pro allows 3
        self.make_paid_sub(plan=self.business)
        response = self.client.post(f"{BASE}/change-plan", {"plan_key": "pro"}, format="json")
        self.assertEqual(response.status_code, 409)
        mocked.assert_not_called()

    @patch("apps.billing.services.dodo.change_plan")
    def test_change_rejected_without_active_subscription(self, mocked, _seats, _wa):
        response = self.client.post(f"{BASE}/change-plan", {"plan_key": "pro"}, format="json")
        self.assertEqual(response.status_code, 409)
        mocked.assert_not_called()

    @patch("apps.billing.services.dodo.change_plan")
    def test_change_rejected_when_cancellation_scheduled(self, mocked, _seats, _wa):
        self.make_paid_sub(cancel_at_period_end=True)
        response = self.client.post(f"{BASE}/change-plan", {"plan_key": "business"}, format="json")
        self.assertEqual(response.status_code, 409)
        mocked.assert_not_called()

    @patch("apps.billing.services.dodo.change_plan")
    def test_cannot_change_to_free(self, mocked, _seats, _wa):
        self.make_paid_sub()
        response = self.client.post(f"{BASE}/change-plan", {"plan_key": "free"}, format="json")
        self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()


class CancelAndSeatsApiTests(BillingTestBase):
    @patch("apps.billing.services.dodo.cancel_subscription")
    def test_cancel_schedules_at_period_end(self, mocked):
        sub = self.make_paid_sub()
        response = self.client.post(f"{BASE}/subscription/cancel", {}, format="json")
        self.assertEqual(response.status_code, 200)
        mocked.assert_called_once_with("sub_1")

        sub.refresh_from_db()
        self.assertTrue(sub.cancel_at_period_end)
        self.assertEqual(sub.plan_id, self.pro.id)  # keeps access until period ends
        self.assertEqual(sub.status, STATUS_ACTIVE)

    @patch("apps.billing.services.dodo.cancel_subscription")
    def test_cancel_twice_is_rejected(self, mocked):
        self.make_paid_sub(cancel_at_period_end=True)
        response = self.client.post(f"{BASE}/subscription/cancel", {}, format="json")
        self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()

    @patch("apps.billing.services.dodo.cancel_subscription")
    def test_cancel_without_subscription_is_rejected(self, mocked):
        response = self.client.post(f"{BASE}/subscription/cancel", {}, format="json")
        self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()

    @patch("apps.billing.services.dodo.change_plan")
    def test_seats_reject_bad_input(self, mocked):
        self.make_paid_sub()
        for value in ("abc", -1, 1000000):
            with self.subTest(value=value):
                response = self.client.post(f"{BASE}/seats", {"extra_seats": value}, format="json")
                self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()

    @patch("apps.billing.views.seats_used", return_value=6)
    @patch("apps.billing.services.dodo.change_plan")
    def test_seats_cannot_drop_below_active_agents(self, mocked, _seats):
        self.make_paid_sub(extra_seats=3)  # limit 6
        response = self.client.post(f"{BASE}/seats", {"extra_seats": 0}, format="json")  # limit 3
        self.assertEqual(response.status_code, 409)
        mocked.assert_not_called()

    @patch("apps.billing.views.seats_used", return_value=1)
    @patch("apps.billing.services.dodo.change_plan")
    def test_seats_use_the_orgs_billing_interval_product(self, mocked, _seats):
        self.make_paid_sub(billing_interval="yearly")
        response = self.client.post(f"{BASE}/seats", {"extra_seats": 2}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocked.call_args.kwargs["product_id"], "pdt_pro_y")
        self.assertEqual(mocked.call_args.kwargs["extra_seats"], 2)


@override_settings(DODO_ADDON_SEAT="addon_seat")
class WebhookTests(BillingTestBase):
    def setUp(self):
        super().setUp()
        self.anon = Client()  # webhooks are called by Dodo, not a logged-in user

    def sub_event(self, type_="subscription.active", **data_overrides):
        data = {
            "subscription_id": "sub_1",
            "customer": {"customer_id": "cus_1"},
            "product_id": "pdt_pro_y",
            "metadata": {"organization_id": str(self.org.id)},
            "addons": [{"addon_id": "addon_seat", "quantity": 2}],
            "next_billing_date": "2026-10-20T00:00:00Z",
            "previous_billing_date": "2026-09-20T00:00:00Z",
        }
        data.update(data_overrides)
        return {"type": type_, "data": data}

    def deliver(self, event, webhook_id="evt_1"):
        with patch("apps.billing.services.dodo.verify_webhook", return_value=event):
            return self.anon.post(
                WEBHOOK_URL,
                data=json.dumps(event),
                content_type="application/json",
                HTTP_WEBHOOK_ID=webhook_id,
            )

    # ---- signature ----
    def test_invalid_signature_is_rejected_and_nothing_is_stored(self):
        with patch(
            "apps.billing.services.dodo.verify_webhook",
            side_effect=ValueError("Invalid webhook signature"),
        ):
            response = self.anon.post(
                WEBHOOK_URL, data="{}", content_type="application/json", HTTP_WEBHOOK_ID="evt_x"
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(DodoWebhookEvent.objects.count(), 0)
        self.assertFalse(OrganizationSubscription.objects.exists())

    @override_settings(DODO_WEBHOOK_SECRET="whsec_dGVzdC1zZWNyZXQ=")
    def test_real_verifier_rejects_a_forged_signature(self):
        response = self.anon.post(
            WEBHOOK_URL,
            data=json.dumps(self.sub_event()),
            content_type="application/json",
            HTTP_WEBHOOK_ID="evt_forged",
            HTTP_WEBHOOK_TIMESTAMP="1700000000",
            HTTP_WEBHOOK_SIGNATURE="v1,Zm9yZ2Vk",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(DodoWebhookEvent.objects.count(), 0)

    def test_missing_webhook_id_is_rejected(self):
        event = self.sub_event()
        with patch("apps.billing.services.dodo.verify_webhook", return_value=event):
            response = self.anon.post(
                WEBHOOK_URL, data=json.dumps(event), content_type="application/json"
            )
        self.assertEqual(response.status_code, 400)

    # ---- success / renewal ----
    def test_subscription_active_applies_plan_interval_seats_and_customer(self):
        response = self.deliver(self.sub_event())
        self.assertEqual(response.status_code, 200)

        sub = OrganizationSubscription.objects.get(organization=self.org)
        self.assertEqual(sub.plan_id, self.pro.id)
        self.assertEqual(sub.billing_interval, "yearly")
        self.assertEqual(sub.extra_seats, 2)
        self.assertEqual(sub.status, STATUS_ACTIVE)
        self.assertEqual(sub.dodo_customer_id, "cus_1")
        self.assertEqual(sub.dodo_subscription_id, "sub_1")

    def test_plan_changed_moves_to_new_plan(self):
        self.make_paid_sub(plan=self.pro)
        self.deliver(self.sub_event("subscription.plan_changed", product_id="pdt_biz_m"))
        sub = OrganizationSubscription.objects.get(organization=self.org)
        self.assertEqual(sub.plan_id, self.business.id)
        self.assertEqual(sub.billing_interval, "monthly")

    def test_renewal_keeps_subscription_active(self):
        self.make_paid_sub()
        self.deliver(self.sub_event("subscription.renewed", product_id="pdt_pro_m"))
        sub = OrganizationSubscription.objects.get(organization=self.org)
        self.assertEqual(sub.status, STATUS_ACTIVE)
        self.assertIsNotNone(sub.current_period_end)

    def test_cancel_at_next_billing_date_flag_is_synced(self):
        # e.g. the customer cancelled from Dodo's own portal, not from our API
        self.make_paid_sub()
        self.deliver(self.sub_event("subscription.renewed", cancel_at_next_billing_date=True))
        sub = OrganizationSubscription.objects.get(organization=self.org)
        self.assertTrue(sub.cancel_at_period_end)

    # ---- failure / cancellation ----
    def test_on_hold_marks_status(self):
        self.make_paid_sub()
        self.deliver(self.sub_event("subscription.on_hold"))
        self.assertEqual(OrganizationSubscription.objects.get(organization=self.org).status, "on_hold")

    def test_cancelled_returns_org_to_free_and_clears_extras(self):
        self.make_paid_sub(extra_seats=2, cancel_at_period_end=True)
        self.deliver(self.sub_event("subscription.cancelled"))
        sub = OrganizationSubscription.objects.get(organization=self.org)
        self.assertEqual(sub.status, STATUS_CANCELLED)
        self.assertEqual(sub.plan_id, self.free.id)
        self.assertEqual(sub.extra_seats, 0)
        self.assertFalse(sub.cancel_at_period_end)

    def test_expired_is_treated_like_cancelled(self):
        self.make_paid_sub()
        self.deliver(self.sub_event("subscription.expired"))
        sub = OrganizationSubscription.objects.get(organization=self.org)
        self.assertEqual(sub.plan_id, self.free.id)

    # ---- refunds and payments ----
    def test_refund_events_are_accepted_and_stored(self):
        for i, type_ in enumerate(("refund.succeeded", "refund.failed")):
            event = {"type": type_, "data": {"payment_id": "pay_1", "amount": 2900}}
            response = self.deliver(event, webhook_id=f"evt_refund_{i}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["type"], type_)
        self.assertEqual(DodoWebhookEvent.objects.count(), 2)

    def test_payment_events_are_accepted(self):
        self.make_paid_sub()
        for i, type_ in enumerate(("payment.succeeded", "payment.failed")):
            response = self.deliver(
                {
                    "type": type_,
                    "data": {
                        "payment_id": f"pay_{i}",
                        "subscription_id": "sub_1",
                        "customer_id": "cus_1",
                        "total_amount": 2900,
                        "currency": "usd",
                    },
                },
                f"evt_pay_{i}",
            )
            self.assertEqual(response.status_code, 200)
        payments = {payment.payment_id: payment for payment in BillingPayment.objects.all()}
        self.assertEqual(payments["pay_0"].status, "succeeded")
        self.assertEqual(payments["pay_0"].amount, 2900)
        self.assertEqual(payments["pay_0"].organization_id, self.org.id)
        self.assertEqual(payments["pay_1"].status, "failed")

    def test_failed_payment_marks_active_subscription_failed(self):
        self.make_paid_sub()
        response = self.deliver(
            {
                "type": "payment.failed",
                "data": {"payment_id": "pay_failed", "subscription_id": "sub_1"},
            },
            "evt_pay_failed",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            OrganizationSubscription.objects.get(organization=self.org).status,
            STATUS_FAILED,
        )

    def test_unknown_event_types_are_stored_but_ignored(self):
        response = self.deliver({"type": "license_key.created", "data": {}}, "evt_lic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DodoWebhookEvent.objects.filter(webhook_id="evt_lic").count(), 1)

    def test_refund_updates_the_payment_record(self):
        self.deliver(
            {"type": "payment.succeeded", "data": {"payment_id": "pay_refund"}},
            "evt_pay_refund",
        )
        self.deliver(
            {
                "type": "refund.succeeded",
                "data": {"payment_id": "pay_refund", "amount": 1000},
            },
            "evt_refund_payment",
        )
        payment = BillingPayment.objects.get(payment_id="pay_refund")
        self.assertEqual(payment.refund_status, "succeeded")
        self.assertEqual(payment.refund_amount, 1000)

    # ---- idempotency ----
    def test_duplicate_delivery_is_processed_once(self):
        first = self.deliver(self.sub_event(), webhook_id="evt_dup")
        second = self.deliver(self.sub_event(), webhook_id="evt_dup")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["data"].get("duplicate"))
        self.assertEqual(DodoWebhookEvent.objects.filter(webhook_id="evt_dup").count(), 1)

    def test_failed_processing_is_rolled_back_so_the_retry_works(self):
        event = self.sub_event()
        with patch("apps.billing.webhooks._process_event", side_effect=RuntimeError("boom")):
            crashed = self.deliver(event, webhook_id="evt_retry")
        self.assertEqual(crashed.status_code, 500)
        self.assertEqual(DodoWebhookEvent.objects.count(), 0)  # not recorded as seen

        retried = self.deliver(event, webhook_id="evt_retry")  # Dodo retries
        self.assertEqual(retried.status_code, 200)
        self.assertFalse(retried.json()["data"].get("duplicate", False))
        sub = OrganizationSubscription.objects.get(organization=self.org)
        self.assertEqual(sub.plan_id, self.pro.id)

    # ---- trust boundary ----
    def test_unknown_product_does_not_change_the_plan(self):
        self.make_paid_sub(plan=self.pro)
        self.deliver(self.sub_event("subscription.plan_changed", product_id="pdt_mystery"))
        sub = OrganizationSubscription.objects.get(organization=self.org)
        self.assertEqual(sub.plan_id, self.pro.id)


class PaymentProfileTenantTests(BillingTestBase):
    data = {
        "billing_name": "Admin User",
        "billing_email": "admin@example.com",
        "country": "Nepal",
    }

    def test_profile_is_always_created_for_the_token_org(self):
        response = self.client.post(
            f"{BASE}/payment-profile/create",
            {**self.data, "organization": str(self.other_org.id), "organization_id": str(self.other_org.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(PaymentProfile.objects.filter(organization_id=self.org.id).exists())
        self.assertFalse(PaymentProfile.objects.filter(organization_id=self.other_org.id).exists())

    def test_org_cannot_read_another_orgs_profile(self):
        PaymentProfile.objects.create(organization_id=self.other_org.id, **self.data)
        response = self.client.get(f"{BASE}/payment-profile")
        self.assertEqual(response.status_code, 404)