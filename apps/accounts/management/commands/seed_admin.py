import bcrypt
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction

from apps.accounts.models import CustomRole, Organization, User, UserOrganization


class Command(BaseCommand):
    help = "Create admin@admin.com / admin if the users table exists and that email is missing."

    def handle(self, *args, **options):
        try:
            if User.objects.filter(email="admin@admin.com").exists():
                self.stdout.write(self.style.SUCCESS("Admin user already exists (admin@admin.com)."))
                return
        except DatabaseError as exc:
            raise CommandError(
                "Postgres has no product tables yet. Use the shared team database "
                f"(schema already applied). Original error: {exc}"
            ) from exc

        org = Organization.objects.order_by("created_at").first()
        if org is None:
            org = Organization.objects.create(name="Default Organization", slug="default", settings={})

        role = (
            CustomRole.objects.filter(organization=org, name="admin", is_system=True).first()
            or CustomRole.objects.filter(organization=org, is_default=True).first()
        )
        password_hash = bcrypt.hashpw(b"admin", bcrypt.gensalt(rounds=10)).decode("utf-8")
        with transaction.atomic():
            user = User.objects.create(
                organization=org,
                email="admin@admin.com",
                password_hash=password_hash,
                full_name="Admin",
                role_id=role.id if role else None,
                settings={},
                is_active=True,
                is_available=True,
                is_super_admin=True,
            )
            if role:
                UserOrganization.objects.get_or_create(
                    user=user,
                    organization=org,
                    defaults={"role_id": role.id, "is_default": True},
                )
        self.stdout.write(self.style.SUCCESS("Created admin@admin.com / admin"))
