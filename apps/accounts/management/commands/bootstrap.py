from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "migrate + seed_admin (schema, permissions, admin user, billing) against DATABASE_URL or POSTGRES_*."

    def handle(self, *args, **options):
        call_command("migrate", interactive=False)
        call_command("seed_admin")
        self.stdout.write(self.style.SUCCESS("Bootstrap complete"))
