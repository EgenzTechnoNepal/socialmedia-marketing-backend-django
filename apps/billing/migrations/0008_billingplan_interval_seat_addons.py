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
    ]
