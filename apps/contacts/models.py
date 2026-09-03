from django.db import models

from apps.accounts.models import Organization, User, UUIDModel, ActiveManager


class Contact(UUIDModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.DO_NOTHING, db_constraint=False
    )
    phone_number = models.CharField(max_length=50)
    profile_name = models.CharField(max_length=255, blank=True)
    whatsapp_account = models.CharField(max_length=100, blank=True, db_column="whats_app_account")
    assigned_user = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False
    )
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_message_preview = models.TextField(blank=True)
    is_read = models.BooleanField(default=True)
    tags = models.JSONField(default=list)
    metadata = models.JSONField(default=dict)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    marketing_opt_out = models.BooleanField(default=False)
    bs_uid = models.CharField(max_length=150, blank=True)
    chatbot_last_message_at = models.DateTimeField(null=True, blank=True)
    chatbot_reminder_sent = models.BooleanField(default=False)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "contacts"
        managed = False


class Tag(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.DO_NOTHING, db_constraint=False, primary_key=False
    )
    name = models.CharField(max_length=50, primary_key=True)
    color = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tags"
        managed = False
        unique_together = ("organization", "name")


class ConversationNote(UUIDModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.DO_NOTHING, db_constraint=False
    )
    contact = models.ForeignKey(Contact, on_delete=models.DO_NOTHING, db_constraint=False)
    created_by = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_constraint=False)
    content = models.TextField()

    objects = ActiveManager()

    class Meta:
        db_table = "conversation_notes"
        managed = False
