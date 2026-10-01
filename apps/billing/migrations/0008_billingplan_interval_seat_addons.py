from django.db import migrations, models


def add_missing_seat_addon_columns(apps, schema_editor):
    """Support databases where these columns were added before migration 0008 ran."""
    plan_model = apps.get_model("billing", "BillingPlan")
    table = plan_model._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        existing = {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(cursor, table)
        }

    for name in ("dodo_seat_addon_id_monthly", "dodo_seat_addon_id_yearly"):
        if name in existing:
            continue
        field = models.CharField(blank=True, max_length=128)
        field.contribute_to_class(plan_model, name)
        schema_editor.add_field(plan_model, field)


def backfill_dodo_skus(apps, schema_editor):
    BillingPlan = apps.get_model("billing", "BillingPlan")
    for plan in BillingPlan.objects.all().iterator():
        updates = {}
        currency = (plan.currency or "USD").lower()
        for interval, price_field, id_field, sku_field, prefix in (
            ("monthly", "price_monthly", "dodo_price_id_monthly", "dodo_sku_monthly", "plan"),
            ("yearly", "price_yearly", "dodo_price_id_yearly", "dodo_sku_yearly", "plan"),
            ("monthly", "extra_seat_price_monthly", "dodo_seat_addon_id_monthly", "dodo_sku_seat_monthly", "seat"),
            ("yearly", "extra_seat_price_yearly", "dodo_seat_addon_id_yearly", "dodo_sku_seat_yearly", "seat"),
        ):
            if getattr(plan, id_field) and not getattr(plan, sku_field):
                updates[sku_field] = (
                    f"whatomate:{prefix}:{plan.key.lower()}:{interval}:{currency}:"
                    f"{getattr(plan, price_field)}"
                )
        if updates:
            BillingPlan.objects.filter(pk=plan.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [("billing", "0007_billingpayment")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(add_missing_seat_addon_columns, migrations.RunPython.noop)],
            state_operations=[
                migrations.AddField(
                    model_name="billingplan",
                    name="dodo_seat_addon_id_monthly",
                    field=models.CharField(blank=True, max_length=128),
                ),
                migrations.AddField(
                    model_name="billingplan",
                    name="dodo_seat_addon_id_yearly",
                    field=models.CharField(blank=True, max_length=128),
                ),
            ],
        ),
        migrations.AddField(
            model_name="billingplan",
            name="dodo_sku_monthly",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="billingplan",
            name="dodo_sku_yearly",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="billingplan",
            name="dodo_sku_seat_monthly",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="billingplan",
            name="dodo_sku_seat_yearly",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="billingplan",
            name="dodo_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="billingplan",
            name="dodo_sync_error",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="billingplan",
            name="dodo_former_product_ids",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(backfill_dodo_skus, migrations.RunPython.noop),
    ]
