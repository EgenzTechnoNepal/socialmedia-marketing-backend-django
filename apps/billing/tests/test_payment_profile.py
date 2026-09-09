from uuid import uuid4

from django.test import TestCase
from rest_framework.test import APIClient

from apps.billing.models import PaymentProfile


class MockUser:
    is_authenticated = True
    is_super_admin = True

    def __init__(self, organization_id):
        self.organization_id = organization_id
        self.token_organization_id = organization_id
        self.is_super_admin_claim = True

    def has_permission(self, resource, action):
        return True


class PaymentProfileAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.organization_id = uuid4()
        self.other_organization_id = uuid4()

        self.user = MockUser(self.organization_id)

        self.client.force_authenticate(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Bearer test-token"
        )

        self.create_url = "/api/billing/payment-profile/create"
        self.detail_url = "/api/billing/payment-profile"
        self.update_url = "/api/billing/payment-profile/update"

        self.valid_data = {
            "billing_name": "Admin User",
            "billing_email": "admin@example.com",
            "phone": "9800000000",
            "address": "Kathmandu",
            "city": "Kathmandu",
            "state": "Bagmati",
            "postal_code": "44600",
            "country": "Nepal",
            "tax_id": "",
        }

    def test_create_payment_profile(self):
        response = self.client.post(
            self.create_url,
            self.valid_data,
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "success")

        data = response.data["data"]

        self.assertEqual(data["billing_name"], "Admin User")
        self.assertEqual(data["billing_email"], "admin@example.com")
        self.assertEqual(data["country"], "Nepal")

        self.assertTrue(
            PaymentProfile.objects.filter(
                organization_id=self.organization_id
            ).exists()
        )

    def test_create_duplicate_payment_profile(self):
        PaymentProfile.objects.create(
            organization_id=self.organization_id,
            **self.valid_data,
        )

        response = self.client.post(
            self.create_url,
            self.valid_data,
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["status"], "error")
        self.assertEqual(
            response.data["message"],
            "Payment profile already exists",
        )

    def test_get_payment_profile(self):
        profile = PaymentProfile.objects.create(
            organization_id=self.organization_id,
            **self.valid_data,
        )

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "success")
        self.assertEqual(
            response.data["data"]["id"],
            str(profile.id),
        )
        self.assertEqual(
            response.data["data"]["billing_email"],
            "admin@example.com",
        )

    def test_get_payment_profile_not_found(self):
        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["status"], "error")
        self.assertEqual(
            response.data["message"],
            "Payment profile not found",
        )

    def test_update_payment_profile(self):
        PaymentProfile.objects.create(
            organization_id=self.organization_id,
            **self.valid_data,
        )

        response = self.client.patch(
            self.update_url,
            {
                "billing_name": "Updated Admin",
                "billing_email": "UPDATED@EXAMPLE.COM",
                "city": "Lalitpur",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "success")

        data = response.data["data"]

        self.assertEqual(data["billing_name"], "Updated Admin")
        self.assertEqual(
            data["billing_email"],
            "updated@example.com",
        )
        self.assertEqual(data["city"], "Lalitpur")

    def test_update_payment_profile_not_found(self):
        response = self.client.patch(
            self.update_url,
            {
                "billing_name": "Updated Admin",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["status"], "error")
        self.assertEqual(
            response.data["message"],
            "Payment profile not found",
        )

    def test_create_payment_profile_invalid_email(self):
        invalid_data = self.valid_data.copy()
        invalid_data["billing_email"] = "not-an-email"

        response = self.client.post(
            self.create_url,
            invalid_data,
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["status"], "error")
        self.assertEqual(
            response.data["message"],
            "Validation failed",
        )

    def test_payment_profile_is_organization_scoped(self):
        PaymentProfile.objects.create(
            organization_id=self.other_organization_id,
            **self.valid_data,
        )

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["status"], "error")
        self.assertEqual(
            response.data["message"],
            "Payment profile not found",
        )