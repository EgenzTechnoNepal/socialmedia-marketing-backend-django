from django.db import models

from apps.accounts.models import ActiveManager, Organization, UUIDModel


class Webhook(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    name = models.CharField(max_length=255)
    url = models.TextField()
    events = models.JSONField(default=list)
    headers = models.JSONField(default=dict)
    secret = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "webhooks"
        managed = False


class CustomAction(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    name = models.CharField(max_length=100)
    icon = models.CharField(max_length=50, blank=True)
    action_type = models.CharField(max_length=20)
    config = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(default=0)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "custom_actions"
        managed = False
