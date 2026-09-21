import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0006_billingplan_seat_price_wa_accounts"),
    ]

    operations = [
        migrations.CreateModel(
            name="BillingPayment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("payment_id", models.CharField(max_length=128, unique=True)),
                ("subscription_id", models.CharField(blank=True, max_length=128)),
                ("customer_id", models.CharField(blank=True, max_length=128)),
                ("status", models.CharField(max_length=32)),
                ("amount", models.PositiveIntegerField(blank=True, null=True)),
                ("currency", models.CharField(blank=True, max_length=3)),
                ("refund_status", models.CharField(blank=True, max_length=32)),
                ("refund_amount", models.PositiveIntegerField(blank=True, null=True)),
                ("payload", models.JSONField(default=dict)),
                ("occurred_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="billing_payments", to="accounts.organization")),
            ],
            options={"db_table": "billing_payments"},
        ),
    ]