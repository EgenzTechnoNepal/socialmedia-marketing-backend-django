from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from apps.accounts.models import Organization
from apps.billing.models import (
    DEFAULT_FEATURES,
    DEFAULT_QUOTAS,
    PLAN_BUSINESS,
    PLAN_FREE,
    PLAN_PRO,
    STATUS_FREE,
    BillingPlan,
    OrganizationSubscription,
)


PLANS = [
    {
        "key": PLAN_FREE,
        "name": "Free",
        "description": "1 seat, limited messages, no campaigns or AI.",
        "included_seats": 1,
        "display_order": 0,
        "currency": "USD",
        "price_monthly": 0,
        "price_yearly": 0,
        "product_env": "",
        "price_monthly_env": "",
        "price_yearly_env": "",
        "extra_seat_price_monthly": 0,
        "extra_seat_price_yearly": 0,
        "included_wa_accounts": 1,
    },
    {
        "key": PLAN_PRO,
        "name": "Pro",
        "description": "3 seats and 2 WhatsApp accounts included, campaigns, AI, calling, usage overage.",
        "included_seats": 3,
        "display_order": 1,
        "currency": "USD",
        "price_monthly": 2900,
        "price_yearly": 29000,
        "product_env": "DODO_PRODUCT_PRO",
        "price_monthly_env": "DODO_PRICE_PRO_MONTHLY",
        "price_yearly_env": "DODO_PRICE_PRO_YEARLY",
        "extra_seat_price_monthly":500,
        "extra_seat_price_yearly":5000,
        "included_wa_accounts": 2,
    },
    {
        "key": PLAN_BUSINESS,
        "name": "Business",
        "description": "10 seats included, higher usage, all features.",
        "included_seats": 10,
        "display_order": 2,
        "currency": "USD",
        "price_monthly": 9900,
        "price_yearly": 99000,
        "product_env": "DODO_PRODUCT_BUSINESS",
        "price_monthly_env": "DODO_PRICE_BUSINESS_MONTHLY",
        "price_yearly_env": "DODO_PRICE_BUSINESS_YEARLY",
        "extra_seat_price_monthly": 500,
        "extra_seat_price_yearly": 5000,
        "included_wa_accounts": 10,
    },
]


class Command(BaseCommand):
    help = "Seed billing plans, free subscriptions, and settings.billing permissions"

    def handle(self, *args, **options):
        self._seed_plans()
        self._seed_subscriptions()
        self._seed_permission()
        self.stdout.write(self.style.SUCCESS("Billing seed complete"))

    def _seed_plans(self):
        addon = settings.DODO_ADDON_SEAT
        for spec in PLANS:
            existing = BillingPlan.objects.filter(key=spec["key"]).first()
            product_id = getattr(settings, spec["product_env"], "") if spec["product_env"] else ""
            price_id_monthly = (
                getattr(settings, spec["price_monthly_env"], "")
                if spec["price_monthly_env"] else ""
            )
            price_id_yearly = (
                getattr(settings, spec["price_yearly_env"], "")
                if spec["price_yearly_env"] else ""
            )
            plan, created = BillingPlan.objects.update_or_create(
                key=spec["key"],
                defaults={
                    "name": spec["name"],
                    "description": spec["description"],
                    "included_seats": spec["included_seats"],
                    "features": DEFAULT_FEATURES[spec["key"]],
                    "included_quotas": DEFAULT_QUOTAS[spec["key"]],
                    "display_order": spec["display_order"],
                    "currency": spec["currency"],
                    "price_monthly": spec["price_monthly"],
                    "price_yearly": spec["price_yearly"],
                    "dodo_product_id": product_id or (existing.dodo_product_id if existing else ""),
                    "dodo_price_id_monthly": price_id_monthly or (existing.dodo_price_id_monthly if existing else ""),
                    "dodo_price_id_yearly": price_id_yearly or (existing.dodo_price_id_yearly if existing else ""),
                    "dodo_seat_addon_id": (
                        addon or (existing.dodo_seat_addon_id if existing else "")
                        if spec["key"] != PLAN_FREE else ""
                    ),
                    "is_active": True,
                    "extra_seat_price_monthly": spec["extra_seat_price_monthly"],
                    "extra_seat_price_yearly": spec["extra_seat_price_yearly"],
                    "included_wa_accounts": spec["included_wa_accounts"],
                },
            )
            self.stdout.write(f"  plan {plan.key} ({'created' if created else 'updated'})")
            if plan.is_paid:
                for interval in ("monthly", "yearly"):
                    if plan.price_for_interval(interval) <= 0:
                        self.stdout.write(self.style.WARNING(
                            f"    ! {plan.key}: {interval} price is zero, checkout unavailable for {interval}"
                        ))
    def _seed_subscriptions(self):
        free = BillingPlan.objects.get(key=PLAN_FREE)
        for org in Organization.objects.all():
            _, created = OrganizationSubscription.objects.get_or_create(
                organization=org,
                defaults={"plan": free, "status": STATUS_FREE},
            )
            if created:
                self.stdout.write(f"  free subscription for org {org.slug}")

    def _seed_permission(self):
        now = timezone.now()
        with connection.cursor() as cursor:
            for action, desc in (("read", "View billing"), ("write", "Manage billing and seats")):
                cursor.execute(
                    """
                    INSERT INTO permissions (id, created_at, updated_at, resource, action, description)
                    SELECT gen_random_uuid(), %s, %s, 'settings.billing', %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM permissions
                        WHERE resource = 'settings.billing' AND action = %s AND deleted_at IS NULL
                    )
                    """,
                    [now, now, action, desc, action],
                )
            cursor.execute(
                """
                INSERT INTO role_permissions (custom_role_id, permission_id)
                SELECT cr.id, p.id
                FROM custom_roles cr
                JOIN permissions p ON p.resource = 'settings.billing' AND p.deleted_at IS NULL
                WHERE cr.is_system = TRUE AND LOWER(cr.name) IN ('admin', 'manager')
                AND NOT EXISTS (
                    SELECT 1 FROM role_permissions rp
                    WHERE rp.custom_role_id = cr.id AND rp.permission_id = p.id
                )
                """
            )
        self.stdout.write("  settings.billing permission granted to system admin/manager roles")
