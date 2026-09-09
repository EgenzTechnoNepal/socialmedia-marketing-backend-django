from rest_framework import serializers

from .models import PaymentProfile


class PaymentProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentProfile
        fields = [
            "id",
            "billing_name",
            "billing_email",
            "phone",
            "address",
            "city",
            "state",
            "postal_code",
            "country",
            "tax_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
        ]

    def validate_billing_email(self, value):
        return value.strip().lower()

    def validate_country(self, value):
        return value.strip()

    def validate_postal_code(self, value):
        return value.strip()