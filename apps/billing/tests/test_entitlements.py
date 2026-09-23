from uuid import uuid4

from django.test import TransactionTestCase

from apps.accounts.models import Organization
from apps.billing.entitlements import (
    FEATURE_AI,
    FEATURE_AUDIT_LOGS,
    FEATURE_API_KEYS,
    FEATURE_CALLING,
    FEATURE_CAMPAIGNS,
    FEATURE_CUSTOM_ACTIONS,
    FEATURE_CUSTOM_ROLES,
    FEATURE_EXTRA_WA,
    FEATURE_SSO,
    FEATURE_TEAMS_ADVANCED,
    FEATURE_TEAMS_BASIC,
    FEATURE_WEBHOOKS,
    METER_CAMPAIGN_RECIPIENT,
    METER_MESSAGE_SENT,
    assert_feature,
    assert_quota_available,
    current_usage,
    get_or_create_subscription,
    period_start,
    quota_for,
    record_usage,
)
from apps.billing.models import (
    DEFAULT_FEATURES,
    DEFAULT_QUOTAS,
    PLAN_BUSINESS,
    PLAN_FREE,
    PLAN_PRO,
    STATUS_ACTIVE,
    BillingPlan,
    OrganizationSubscription,
    UsageCounter,
)
from apps.common.exceptions import FeatureEntitlementError, QuotaExceededError
from apps.common.schema import apply_product_schema


class EntitlementTestCase(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        apply_product_schema()


    def setUp(self):
        suffix = uuid4().hex[:6]

        self.org = Organization.objects.create(
            name=f"Entitlement Test Org {suffix}",
            slug=f"entitlement-test-org-{suffix}",
        )

        self.free, _ = BillingPlan.objects.update_or_create(
            key=PLAN_FREE,
            defaults={
                "name": "Free",
                "included_seats": 1,
                "features": DEFAULT_FEATURES[PLAN_FREE],
                "included_quotas": DEFAULT_QUOTAS[PLAN_FREE],
            },
        )

        self.pro, _ = BillingPlan.objects.update_or_create(
            key=PLAN_PRO,
            defaults={
                "name": "Pro",
                "included_seats": 3,
                "features": DEFAULT_FEATURES[PLAN_PRO],
                "included_quotas": DEFAULT_QUOTAS[PLAN_PRO],
            },
        )

        self.business, _ = BillingPlan.objects.update_or_create(
            key=PLAN_BUSINESS,
            defaults={
                "name": "Business",
                "included_seats": 10,
                "features": DEFAULT_FEATURES[PLAN_BUSINESS],
                "included_quotas": DEFAULT_QUOTAS[PLAN_BUSINESS],
            },
        )

    def set_subscription(self, plan, status=STATUS_ACTIVE):
        return OrganizationSubscription.objects.create(
            organization=self.org,
            plan=plan,
            status=status,
        )

    def test_free_blocks_campaigns(self):
        self.set_subscription(self.free)

        with self.assertRaises(FeatureEntitlementError) as ctx:
            assert_feature(self.org.id, FEATURE_CAMPAIGNS)

        error = ctx.exception

        self.assertEqual(error.feature_key, FEATURE_CAMPAIGNS)
        self.assertEqual(error.current_plan, PLAN_FREE)
        self.assertEqual(error.required_plan, PLAN_PRO)

    def test_pro_allows_campaigns(self):
        self.set_subscription(self.pro)

        subscription = assert_feature(
            self.org.id,
            FEATURE_CAMPAIGNS,
        )

        self.assertEqual(subscription.plan.key, PLAN_PRO)

    def test_pro_blocks_business_only_feature(self):
        self.set_subscription(self.pro)

        with self.assertRaises(FeatureEntitlementError) as ctx:
            assert_feature(self.org.id, FEATURE_CUSTOM_ROLES)

        error = ctx.exception

        self.assertEqual(error.feature_key, FEATURE_CUSTOM_ROLES)
        self.assertEqual(error.current_plan, PLAN_PRO)
        self.assertEqual(error.required_plan, PLAN_BUSINESS)

    def test_business_allows_business_feature(self):
        self.set_subscription(self.business)

        subscription = assert_feature(
            self.org.id,
            FEATURE_CUSTOM_ROLES,
        )

        self.assertEqual(subscription.plan.key, PLAN_BUSINESS)

    def test_feature_matrix(self):
        features = [
            FEATURE_AI,
            FEATURE_AUDIT_LOGS,
            FEATURE_API_KEYS,
            FEATURE_CALLING,
            FEATURE_CAMPAIGNS,
            FEATURE_CUSTOM_ACTIONS,
            FEATURE_CUSTOM_ROLES,
            FEATURE_EXTRA_WA,
            FEATURE_SSO,
            FEATURE_TEAMS_ADVANCED,
            FEATURE_TEAMS_BASIC,
            FEATURE_WEBHOOKS,
        ]

        pro_allowed = {
            FEATURE_AI,
            FEATURE_CALLING,
            FEATURE_CAMPAIGNS,
            FEATURE_EXTRA_WA,
            FEATURE_TEAMS_BASIC,
        }

        business_only = {
            FEATURE_AUDIT_LOGS,
            FEATURE_API_KEYS,
            FEATURE_CUSTOM_ACTIONS,
            FEATURE_CUSTOM_ROLES,
            FEATURE_SSO,
            FEATURE_TEAMS_ADVANCED,
            FEATURE_WEBHOOKS,
        }

        # Free plan: none of the paid features are allowed.
        for feature in features:
            with self.subTest(plan=PLAN_FREE, feature=feature):
                self.set_subscription(self.free)

                with self.assertRaises(FeatureEntitlementError) as ctx:
                    assert_feature(self.org.id, feature)

                error = ctx.exception
                self.assertEqual(error.feature_key, feature)
                self.assertEqual(error.current_plan, PLAN_FREE)

                expected_plan = (
                    PLAN_BUSINESS
                    if feature in business_only
                    else PLAN_PRO
                )
                self.assertEqual(error.required_plan, expected_plan)

                OrganizationSubscription.objects.filter(
                    organization=self.org
                ).delete()

        # Pro plan: Pro features are allowed, Business-only features are blocked.
        for feature in features:
            with self.subTest(plan=PLAN_PRO, feature=feature):
                self.set_subscription(self.pro)

                if feature in pro_allowed:
                    subscription = assert_feature(self.org.id, feature)
                    self.assertEqual(subscription.plan.key, PLAN_PRO)
                else:
                    with self.assertRaises(FeatureEntitlementError) as ctx:
                        assert_feature(self.org.id, feature)

                    error = ctx.exception
                    self.assertEqual(error.feature_key, feature)
                    self.assertEqual(error.current_plan, PLAN_PRO)
                    self.assertEqual(error.required_plan, PLAN_BUSINESS)

                OrganizationSubscription.objects.filter(
                    organization=self.org
                ).delete()

        # Business plan: all features are allowed.
        self.set_subscription(self.business)

        for feature in features:
            with self.subTest(plan=PLAN_BUSINESS, feature=feature):
                subscription = assert_feature(self.org.id, feature)
                self.assertEqual(subscription.plan.key, PLAN_BUSINESS)

    def test_free_campaign_quota_is_zero(self):
        self.set_subscription(self.free)

        self.assertEqual(
            quota_for(
                get_or_create_subscription(self.org.id),
                METER_CAMPAIGN_RECIPIENT,
            ),
            0,
        )

    def test_free_campaign_quota_exhaustion_raises_429_error(self):
        self.set_subscription(self.free)

        with self.assertRaises(QuotaExceededError) as ctx:
            assert_quota_available(
                self.org.id,
                METER_CAMPAIGN_RECIPIENT,
            )

        error = ctx.exception

        self.assertEqual(error.quota, METER_CAMPAIGN_RECIPIENT)
        self.assertEqual(error.current_plan, PLAN_FREE)

    def test_quota_preflight_does_not_consume_usage(self):
        self.set_subscription(self.free)

        meter = METER_MESSAGE_SENT
        start = period_start()

        UsageCounter.objects.update_or_create(
            organization_id=self.org.id,
            meter=meter,
            period_start=start,
            defaults={"quantity": 0},
        )

        # Free message quota is 100, so one message is available.
        assert_quota_available(
            self.org.id,
            meter,
        )

        self.assertEqual(
            current_usage(self.org.id, meter),
            0,
        )

    def test_record_usage_blocks_free_plan_at_limit(self):
        self.set_subscription(self.free)

        meter = METER_MESSAGE_SENT
        start = period_start()

        UsageCounter.objects.update_or_create(
            organization_id=self.org.id,
            meter=meter,
            period_start=start,
            defaults={"quantity": 100},
        )

        with self.assertRaises(QuotaExceededError):
            record_usage(
                self.org.id,
                meter,
                event_id=f"test:{uuid4()}",
            )

        self.assertEqual(
            current_usage(self.org.id, meter),
            100,
        )

    def test_paid_plan_preflight_allows_included_quota_overage(self):
        self.set_subscription(self.pro)

        meter = METER_MESSAGE_SENT
        start = period_start()

        UsageCounter.objects.update_or_create(
            organization_id=self.org.id,
            meter=meter,
            period_start=start,
            defaults={"quantity": 5000},
        )

        assert_quota_available(
            self.org.id,
            meter,
        )
