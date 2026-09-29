from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.billing.models import BillingPlan
from apps.billing.services import dodo


class Command(BaseCommand):
    help = "Create missing Dodo plan products and seat add-ons"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show catalog items and deterministic keys without calling Dodo or saving IDs",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        plans = BillingPlan.objects.filter(is_active=True).exclude(key="free").order_by("key")
        if dry_run:
            for plan in plans:
                for interval in ("monthly", "yearly"):
                    currency = (plan.currency or "USD").upper()
                    amount = plan.price_for_interval(interval)
                    seat_price = plan.extra_seat_price_for_interval(interval)
                    self.stdout.write(
                        f"{plan.key}/{interval}: product="
                        f"{dodo._catalog_key('whatomate', 'plan', plan.key, interval, currency, amount)} "
                        f"({'exists' if plan.dodo_price_id_for_interval(interval) else 'would create'})"
                    )
                    if seat_price:
                        seat_id = (
                            getattr(plan, f"dodo_seat_addon_id_{interval}")
                            or plan.dodo_seat_addon_id
                            or settings.DODO_ADDON_SEAT
                        )
                        self.stdout.write(
                            f"  seat={dodo._catalog_key('whatomate', 'seat', plan.key, interval, currency, seat_price)} "
                            f"({'exists' if seat_id else 'would create'})"
                        )
            self.stdout.write(self.style.WARNING("Dry run: no Dodo calls or database changes made"))
            return

        if not dodo.is_configured():
            raise CommandError("DODO_PAYMENTS_API_KEY is required unless --dry-run is used")
        for plan in plans:
            for interval in ("monthly", "yearly"):
                try:
                    product_id = dodo.ensure_plan_product(plan, interval)
                    self.stdout.write(f"{plan.key}/{interval}: product {product_id}")
                    if plan.extra_seat_price_for_interval(interval):
                        addon_id = dodo.ensure_seat_addon(plan, interval)
                        self.stdout.write(f"  seat add-on {addon_id}")
                except Exception as exc:
                    raise CommandError(f"Could not sync {plan.key}/{interval}: {exc}") from exc
        self.stdout.write(self.style.SUCCESS("Dodo catalog sync complete"))
