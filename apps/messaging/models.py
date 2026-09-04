from django.db import models

from apps.accounts.models import Organization, User, UUIDModel, ActiveManager
from apps.contacts.models import Contact


class Message(UUIDModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.DO_NOTHING, db_constraint=False
    )
    whatsapp_account = models.CharField(max_length=100, db_column="whats_app_account")
    contact = models.ForeignKey(Contact, on_delete=models.DO_NOTHING, db_constraint=False)
    whatsapp_message_id = models.CharField(
        max_length=255, blank=True, db_column="whats_app_message_id"
    )
    conversation_id = models.CharField(max_length=255, blank=True)
    direction = models.CharField(max_length=10)
    message_type = models.CharField(max_length=20)
    content = models.TextField(blank=True)
    media_url = models.TextField(blank=True)
    media_mime_type = models.CharField(max_length=100, blank=True)
    media_filename = models.CharField(max_length=255, blank=True)
    template_name = models.CharField(max_length=255, blank=True)
    template_params = models.JSONField(null=True, blank=True)
    interactive_data = models.JSONField(null=True, blank=True)
    flow_response = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, default="pending")
    error_message = models.TextField(blank=True)
    is_reply = models.BooleanField(default=False)
    reply_to_message = models.ForeignKey(
        "self", on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False
    )
    sent_by_user = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False
    )
    metadata = models.JSONField(default=dict)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "messages"
        managed = False


class Template(UUIDModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.DO_NOTHING, db_constraint=False
    )
    whatsapp_account = models.CharField(max_length=100, db_column="whats_app_account")
    meta_template_id = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    language = models.CharField(max_length=10)
    category = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, default="PENDING")
    quality_rating = models.CharField(max_length=50, default="UNKNOWN")
    header_type = models.CharField(max_length=20, blank=True)
    header_content = models.TextField(blank=True)
    body_content = models.TextField()
    footer_content = models.TextField(blank=True)
    buttons = models.JSONField(default=list)
    sample_values = models.JSONField(default=list)
    add_security_recommendation = models.BooleanField(default=False)
    code_expiration_minutes = models.IntegerField(default=0)
    created_by = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        db_constraint=False,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        db_constraint=False,
        related_name="+",
    )

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "templates"
        managed = False


class CannedResponse(UUIDModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.DO_NOTHING, db_constraint=False
    )
    name = models.CharField(max_length=100)
    shortcut = models.CharField(max_length=50, blank=True)
    content = models.TextField()
    category = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    usage_count = models.IntegerField(default=0)
    buttons = models.JSONField(default=list)
    created_by = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_constraint=False)

    objects = ActiveManager()

    class Meta:
        db_table = "canned_responses"
        managed = False
