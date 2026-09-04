import bcrypt
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction

from apps.accounts.models import CustomRole, Organization, User, UserOrganization
from apps.accounts.permissions_catalog import seed_permission_catalog
from apps.accounts.roles import seed_system_roles
from apps.common.schema import apply_product_schema, users_table_exists

ADMIN_EMAIL = "admin@admin.com"
ADMIN_PASSWORD = "admin"


class Command(BaseCommand):
    help = (
        "Apply the product schema if needed, seed permissions/roles, "
        "and create admin@admin.com / admin for Vue login."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Reset admin@admin.com password to 'admin' if the user already exists.",
        )

    def handle(self, *args, **options):
        try:
            applied = apply_product_schema()
        except FileNotFoundError as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:
            raise CommandError(f"Failed to apply product schema: {exc}") from exc
        if applied:
            self.stdout.write(self.style.SUCCESS("Applied product tables from backend/sql/product_schema.sql"))

        if not users_table_exists():
            raise CommandError(
                "The users table is still missing after applying schema. "
                "Check DATABASE_URL (or POSTGRES_*) in backend/.env."
            )

        created_perms = seed_permission_catalog()
        if created_perms:
            self.stdout.write(f"Seeded {created_perms} permission rows")

        org = Organization.objects.order_by("created_at").first()
        if org is None:
            org = Organization.objects.create(name="Default Organization", slug="default", settings={})
            self.stdout.write(f"Created organization {org.slug}")

        role = CustomRole.objects.filter(organization=org, name="admin", is_system=True).first()
        if role is None:
            role = seed_system_roles(org)
            self.stdout.write("Seeded admin/manager/agent roles")
        else:
            seed_system_roles(org)

        password_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")
        try:
            user = User.objects.filter(email=ADMIN_EMAIL).first()
        except DatabaseError as exc:
            raise CommandError(f"Cannot query users table: {exc}") from exc

        if user is not None:
            if options["reset"]:
                user.password_hash = password_hash
                user.is_active = True
                user.is_super_admin = True
                user.organization = org
                if role:
                    user.role_id = role.id
                user.save()
                self.stdout.write(self.style.SUCCESS(f"Reset password for {ADMIN_EMAIL} / {ADMIN_PASSWORD}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"Admin user already exists ({ADMIN_EMAIL})."))
        else:
            with transaction.atomic():
                user = User.objects.create(
                    organization=org,
                    email=ADMIN_EMAIL,
                    password_hash=password_hash,
                    full_name="Admin",
                    role_id=role.id if role else None,
                    settings={},
                    is_active=True,
                    is_available=True,
                    is_super_admin=True,
                )
            self.stdout.write(self.style.SUCCESS(f"Created {ADMIN_EMAIL} / {ADMIN_PASSWORD}"))

        if role:
            UserOrganization.objects.get_or_create(
                user=user,
                organization=org,
                defaults={"role_id": role.id, "is_default": True},
            )

        try:
            call_command("seed_billing")
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"seed_billing skipped: {exc}"))

        self.stdout.write(self.style.SUCCESS("Login from Vue at http://localhost:3000 with admin@admin.com / admin"))
