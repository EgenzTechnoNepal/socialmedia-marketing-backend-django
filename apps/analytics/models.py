from django.db import models

from apps.accounts.models import Organization, User, UUIDModel, ActiveManager


class Widget(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    data_source = models.CharField(max_length=50)
    metric = models.CharField(max_length=20)
    field = models.CharField(max_length=100, blank=True)
    filters = models.JSONField(default=list)
    display_type = models.CharField(max_length=20, default="number")
    chart_type = models.CharField(max_length=20, blank=True)
    group_by_field = models.CharField(max_length=100, blank=True)
    show_change = models.BooleanField(default=True)
    color = models.CharField(max_length=20, blank=True)
    size = models.CharField(max_length=10, default="small")
    display_order = models.BigIntegerField(default=0)
    grid_x = models.BigIntegerField(default=0)
    grid_y = models.BigIntegerField(default=0)
    grid_w = models.BigIntegerField(default=0)
    grid_h = models.BigIntegerField(default=0)
    config = models.JSONField(default=dict)
    is_shared = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "widgets"
        managed = False
