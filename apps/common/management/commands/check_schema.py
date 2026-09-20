from django.core.management.base import BaseCommand
from django.db import connection


EXPECTED_TABLES = (
    "agent_transfers",
    "ai_contexts",
    "api_keys",
    "audit_logs",
    "billing_plans",
    "billing_payments",
    "billing_usage_counters",
    "billing_usage_outbox",
    "bulk_message_campaigns",
    "bulk_message_recipients",
    "call_logs",
    "call_permissions",
    "call_transfers",
    "canned_responses",
    "catalog_products",
    "catalogs",
    "chatbot_flow_steps",
    "chatbot_flows",
    "chatbot_session_messages",
    "chatbot_sessions",
    "chatbot_settings",
    "contacts",
    "conversation_notes",
    "custom_actions",
    "custom_roles",
    "dodo_webhook_events",
    "ivr_flows",
    "keyword_rules",
    "messages",
    "notification_rules",
    "organization_subscriptions",
    "organizations",
    "permissions",
    "role_permissions",
    "sso_providers",
    "tags",
    "team_members",
    "teams",
    "templates",
    "user_availability_logs",
    "user_organizations",
    "users",
    "webhooks",
    "whatsapp_accounts",
    "whatsapp_flows",
    "widgets",
)


class Command(BaseCommand):
    help = "Confirm product/billing tables exist on the current DATABASE_URL (no credentials printed)."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )
            present = {row[0] for row in cursor.fetchall()}
        missing = [name for name in EXPECTED_TABLES if name not in present]
        self.stdout.write(f"public tables: {len(present)}")
        if missing:
            self.stderr.write(self.style.ERROR("missing: " + ", ".join(missing)))
            return
        self.stdout.write(self.style.SUCCESS("all expected product tables are present"))
