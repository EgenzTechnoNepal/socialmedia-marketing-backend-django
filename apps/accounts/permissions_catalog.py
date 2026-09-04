"""Permission rows that Go seeds in internal/models/roles.go DefaultPermissions()."""

DEFAULT_PERMISSIONS = [
    ("users", "read", "View users"),
    ("users", "write", "Create and edit users"),
    ("users", "delete", "Delete users"),
    ("teams", "read", "View teams"),
    ("teams", "write", "Create and edit teams"),
    ("teams", "delete", "Delete teams"),
    ("roles", "read", "View roles"),
    ("roles", "write", "Create and edit roles"),
    ("roles", "delete", "Delete roles"),
    ("settings.general", "read", "View general settings"),
    ("settings.general", "write", "Edit general settings"),
    ("settings.chatbot", "read", "View chatbot settings"),
    ("settings.chatbot", "write", "Edit chatbot settings"),
    ("settings.sso", "read", "View SSO settings"),
    ("settings.sso", "write", "Edit SSO settings"),
    ("settings.billing", "read", "View billing"),
    ("settings.billing", "write", "Manage billing and seats"),
    ("accounts", "read", "View WhatsApp accounts"),
    ("accounts", "write", "Create and edit WhatsApp accounts"),
    ("accounts", "delete", "Delete WhatsApp accounts"),
    ("templates", "read", "View message templates"),
    ("templates", "write", "Create and edit templates"),
    ("templates", "delete", "Delete templates"),
    ("templates", "sync", "Sync templates with Meta"),
    ("flows.whatsapp", "read", "View WhatsApp flows"),
    ("flows.whatsapp", "write", "Create and edit WhatsApp flows"),
    ("flows.whatsapp", "delete", "Delete WhatsApp flows"),
    ("flows.chatbot", "read", "View chatbot flows"),
    ("flows.chatbot", "write", "Create and edit chatbot flows"),
    ("flows.chatbot", "delete", "Delete chatbot flows"),
    ("campaigns", "read", "View campaigns"),
    ("campaigns", "write", "Create and edit campaigns"),
    ("campaigns", "delete", "Delete campaigns"),
    ("campaigns", "execute", "Execute campaigns"),
    ("chatbot.keywords", "read", "View keyword rules"),
    ("chatbot.keywords", "write", "Create and edit keyword rules"),
    ("chatbot.keywords", "delete", "Delete keyword rules"),
    ("chatbot.ai", "read", "View AI contexts"),
    ("chatbot.ai", "write", "Create and edit AI contexts"),
    ("chatbot.ai", "delete", "Delete AI contexts"),
    ("chat", "read", "View chat conversations"),
    ("chat", "write", "Send messages"),
    ("chat.assign", "write", "Assign conversations to agents"),
    ("contacts", "read", "View contacts"),
    ("contacts", "write", "Create and edit contacts"),
    ("contacts", "delete", "Delete contacts"),
    ("contacts", "import", "Import contacts"),
    ("contacts", "export", "Export contacts"),
    ("tags", "read", "View tags"),
    ("tags", "write", "Create and edit tags"),
    ("tags", "delete", "Delete tags"),
    ("analytics", "read", "View analytics dashboard"),
    ("analytics", "write", "Create and edit dashboard widgets"),
    ("analytics", "delete", "Delete dashboard widgets"),
    ("analytics.agents", "read", "View agent analytics"),
    ("transfers", "read", "View agent transfers"),
    ("transfers", "write", "Create transfers"),
    ("transfers", "pickup", "Pickup transfers from queue"),
    ("webhooks", "read", "View webhooks"),
    ("webhooks", "write", "Create and edit webhooks"),
    ("webhooks", "delete", "Delete webhooks"),
    ("api_keys", "read", "View API keys"),
    ("api_keys", "write", "Create API keys"),
    ("api_keys", "delete", "Delete API keys"),
    ("canned_responses", "read", "View canned responses"),
    ("canned_responses", "write", "Create and edit canned responses"),
    ("canned_responses", "delete", "Delete canned responses"),
    ("custom_actions", "read", "View custom actions"),
    ("custom_actions", "write", "Create and edit custom actions"),
    ("custom_actions", "delete", "Delete custom actions"),
    ("organizations", "read", "View organizations"),
    ("organizations", "write", "Create organizations"),
    ("organizations", "delete", "Delete organizations"),
    ("organizations", "assign", "Manage organization members"),
    ("call_logs", "read", "View call logs"),
    ("ivr_flows", "read", "View IVR flows"),
    ("ivr_flows", "write", "Create and edit IVR flows"),
    ("ivr_flows", "delete", "Delete IVR flows"),
    ("call_transfers", "read", "View call transfers"),
    ("call_transfers", "write", "Accept and manage call transfers"),
    ("outgoing_calls", "read", "View outgoing call status"),
    ("outgoing_calls", "write", "Initiate outgoing calls"),
    ("audit_logs", "read", "View audit logs"),
]


def seed_permission_catalog() -> int:
    from django.db import connection
    from django.utils import timezone

    now = timezone.now()
    created = 0
    with connection.cursor() as cursor:
        for resource, action, description in DEFAULT_PERMISSIONS:
            cursor.execute(
                """
                INSERT INTO permissions (id, created_at, updated_at, resource, action, description)
                SELECT gen_random_uuid(), %s, %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM permissions
                    WHERE resource = %s AND action = %s AND deleted_at IS NULL
                )
                """,
                [now, now, resource, action, description, resource, action],
            )
            created += cursor.rowcount or 0
    return created
