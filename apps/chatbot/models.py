from django.db import models

from apps.accounts.models import ActiveManager, Organization, User, UUIDModel
from apps.contacts.models import Contact
from apps.messaging.models import Template


class ChatbotSettings(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    whatsapp_account = models.CharField(max_length=100, blank=True, db_column="whats_app_account")
    is_enabled = models.BooleanField(default=False)
    default_response = models.TextField(blank=True)
    greeting_buttons = models.JSONField(default=list)
    fallback_message = models.TextField(blank=True)
    fallback_buttons = models.JSONField(default=list)
    business_hours_enabled = models.BooleanField(default=False)
    business_hours = models.JSONField(default=list)
    out_of_hours_message = models.TextField(blank=True)
    allow_automated_outside_hours = models.BooleanField(default=True)
    allow_agent_queue_pickup = models.BooleanField(default=True)
    assign_to_same_agent = models.BooleanField(default=True)
    agent_current_conversation_only = models.BooleanField(default=False)
    sla_enabled = models.BooleanField(default=False)
    sla_response_minutes = models.IntegerField(default=15)
    sla_resolution_minutes = models.IntegerField(default=60)
    sla_escalation_minutes = models.IntegerField(default=30)
    sla_auto_close_hours = models.IntegerField(default=24)
    sla_auto_close_message = models.TextField(blank=True)
    sla_warning_message = models.TextField(blank=True)
    sla_escalation_notify_ids = models.JSONField(default=list)
    client_reminder_enabled = models.BooleanField(default=False)
    client_reminder_minutes = models.IntegerField(default=30)
    client_reminder_message = models.TextField(blank=True)
    client_auto_close_minutes = models.IntegerField(default=60)
    client_auto_close_message = models.TextField(blank=True)
    ai_enabled = models.BooleanField(default=False)
    ai_provider = models.CharField(max_length=20, blank=True)
    ai_api_key = models.TextField(blank=True)
    ai_model = models.CharField(max_length=100, blank=True)
    ai_max_tokens = models.IntegerField(default=500)
    ai_temperature = models.DecimalField(max_digits=3, decimal_places=2, default=0.7)
    ai_system_prompt = models.TextField(blank=True)
    ai_include_history = models.BooleanField(default=True)
    ai_history_limit = models.IntegerField(default=4)
    session_timeout_mins = models.IntegerField(default=30)
    excluded_numbers = models.JSONField(default=list)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "chatbot_settings"
        managed = False


class KeywordRule(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    whatsapp_account = models.CharField(max_length=100, blank=True, db_column="whats_app_account")
    name = models.CharField(max_length=255)
    is_enabled = models.BooleanField(default=True)
    priority = models.IntegerField(default=10)
    keywords = models.JSONField(default=list)
    match_type = models.CharField(max_length=20, default="contains")
    case_sensitive = models.BooleanField(default=False)
    response_type = models.CharField(max_length=20)
    response_content = models.JSONField(default=dict)
    conditions = models.TextField(blank=True)
    active_from = models.DateTimeField(null=True, blank=True)
    active_until = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False, related_name="+"
    )
    updated_by = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False, related_name="+"
    )

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "keyword_rules"
        managed = False


class ChatbotFlow(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    whatsapp_account = models.CharField(max_length=100, blank=True, db_column="whats_app_account")
    name = models.CharField(max_length=255)
    is_enabled = models.BooleanField(default=True)
    description = models.TextField(blank=True)
    trigger_keywords = models.JSONField(default=list)
    trigger_button_id = models.CharField(max_length=100, blank=True)
    initial_message = models.TextField(blank=True)
    initial_message_type = models.CharField(max_length=20, default="text")
    initial_template = models.ForeignKey(
        Template, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False
    )
    completion_message = models.TextField(blank=True)
    on_complete_action = models.CharField(max_length=20, blank=True)
    completion_config = models.JSONField(null=True, blank=True)
    timeout_message = models.TextField(blank=True)
    cancel_keywords = models.JSONField(default=list)
    panel_config = models.JSONField(default=dict)
    graph = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False, related_name="+"
    )
    updated_by = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False, related_name="+"
    )

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "chatbot_flows"
        managed = False


class ChatbotFlowStep(UUIDModel):
    flow = models.ForeignKey(ChatbotFlow, on_delete=models.DO_NOTHING, db_constraint=False)
    step_name = models.CharField(max_length=100)
    step_order = models.IntegerField()
    message = models.TextField()

    objects = ActiveManager()

    class Meta:
        db_table = "chatbot_flow_steps"
        managed = False


class ChatbotSession(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    contact = models.ForeignKey(Contact, on_delete=models.DO_NOTHING, db_constraint=False)
    whatsapp_account = models.CharField(max_length=100, db_column="whats_app_account")
    phone_number = models.CharField(max_length=50)
    status = models.CharField(max_length=20, default="active")
    current_flow = models.ForeignKey(
        ChatbotFlow, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False
    )
    current_step = models.CharField(max_length=100, blank=True)
    step_retries = models.IntegerField(default=0)
    session_data = models.JSONField(default=dict)
    started_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "chatbot_sessions"
        managed = False


class ChatbotSessionMessage(UUIDModel):
    session = models.ForeignKey(ChatbotSession, on_delete=models.DO_NOTHING, db_constraint=False)
    direction = models.CharField(max_length=10)
    message = models.TextField(blank=True)
    step_name = models.CharField(max_length=100, blank=True)

    objects = ActiveManager()

    class Meta:
        db_table = "chatbot_session_messages"
        managed = False


class AIContext(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    whatsapp_account = models.CharField(max_length=100, blank=True, db_column="whats_app_account")
    name = models.CharField(max_length=255)
    is_enabled = models.BooleanField(default=True)
    priority = models.IntegerField(default=10)
    context_type = models.CharField(max_length=20)
    trigger_keywords = models.JSONField(default=list)
    static_content = models.TextField(blank=True)
    api_config = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False, related_name="+"
    )
    updated_by = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False, related_name="+"
    )

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "ai_contexts"
        managed = False


class Team(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=500, blank=True)
    assignment_strategy = models.CharField(max_length=50, default="round_robin")
    per_agent_timeout_secs = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False, related_name="+"
    )
    updated_by = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False, related_name="+"
    )

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "teams"
        managed = False


class TeamMember(UUIDModel):
    team = models.ForeignKey(Team, on_delete=models.DO_NOTHING, db_constraint=False, related_name="members")
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_constraint=False)
    role = models.CharField(max_length=50, default="agent")
    last_assigned_at = models.DateTimeField(null=True, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "team_members"
        managed = False


class AgentTransfer(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    contact = models.ForeignKey(Contact, on_delete=models.DO_NOTHING, db_constraint=False)
    whatsapp_account = models.CharField(max_length=100, db_column="whats_app_account")
    phone_number = models.CharField(max_length=50)
    status = models.CharField(max_length=20, default="active")
    source = models.CharField(max_length=20, default="manual")
    agent = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False, related_name="+"
    )
    team = models.ForeignKey(Team, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False)
    transferred_by_user = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False, related_name="+"
    )
    notes = models.TextField(blank=True)
    transferred_at = models.DateTimeField(null=True, blank=True)
    resumed_at = models.DateTimeField(null=True, blank=True)
    resumed_by = models.UUIDField(null=True, blank=True)
    sla_response_deadline = models.DateTimeField(null=True, blank=True)
    sla_resolution_deadline = models.DateTimeField(null=True, blank=True)
    sla_escalation_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    escalation_level = models.IntegerField(default=0)
    escalated_at = models.DateTimeField(null=True, blank=True)
    sla_breached = models.BooleanField(default=False)
    sla_breached_at = models.DateTimeField(null=True, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "agent_transfers"
        managed = False


class WhatsAppFlow(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    whatsapp_account = models.CharField(max_length=100, db_column="whats_app_account")
    meta_flow_id = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, default="DRAFT")
    category = models.CharField(max_length=50, blank=True)
    json_version = models.CharField(max_length=10, default="6.0")
    flow_json = models.JSONField(null=True, blank=True)
    screens = models.JSONField(default=list)
    preview_url = models.TextField(blank=True)
    has_local_changes = models.BooleanField(default=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "whatsapp_flows"
        managed = False
