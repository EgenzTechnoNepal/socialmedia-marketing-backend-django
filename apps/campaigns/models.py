from django.db import models

from apps.accounts.models import ActiveManager, Organization, User, UUIDModel
from apps.messaging.models import Message, Template


class BulkMessageCampaign(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    whatsapp_account = models.CharField(max_length=100, db_column="whats_app_account")
    name = models.CharField(max_length=255)
    template = models.ForeignKey(Template, on_delete=models.DO_NOTHING, db_constraint=False)
    header_media_id = models.TextField(blank=True)
    header_media_filename = models.TextField(blank=True)
    header_media_mime_type = models.TextField(blank=True)
    header_media_local_path = models.TextField(blank=True)
    status = models.CharField(max_length=20, default="draft")
    total_recipients = models.IntegerField(default=0)
    sent_count = models.IntegerField(default=0)
    delivered_count = models.IntegerField(default=0)
    read_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, db_column="created_by", db_constraint=False, related_name="+"
    )
    updated_by = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False, related_name="+"
    )

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "bulk_message_campaigns"
        managed = False


class BulkMessageRecipient(UUIDModel):
    campaign = models.ForeignKey(
        BulkMessageCampaign, on_delete=models.DO_NOTHING, db_constraint=False, related_name="recipients"
    )
    phone_number = models.CharField(max_length=50)
    recipient_name = models.CharField(max_length=255, blank=True)
    template_params = models.JSONField(default=dict)
    header_params = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default="pending")
    whatsapp_message_id = models.CharField(max_length=100, blank=True, db_column="whats_app_message_id")
    message = models.ForeignKey(Message, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False)
    error_message = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "bulk_message_recipients"
        managed = False
