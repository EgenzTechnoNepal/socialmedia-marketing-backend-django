from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.billing.services.invoice import (
    build_invoice_data,
    calculate_invoice_totals,
    generate_invoice_pdf,
    minor_to_major,
)

class InvoiceCalculationTests(SimpleTestCase):

    def test_invoice_totals(self):
        result = calculate_invoice_totals(
            subtotal=1000,
            tax=130,
        )

        self.assertEqual(result["subtotal"], Decimal("1000"))
        self.assertEqual(result["tax"], Decimal("130"))
        self.assertEqual(result["total"], Decimal("1130"))

    def test_invoice_without_tax(self):
        result = calculate_invoice_totals(
            subtotal=5000,
            tax=0,
        )

        self.assertEqual(result["total"], Decimal("5000"))

    def test_negative_subtotal_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_invoice_totals(
                subtotal=-100,
                tax=10,
            )

    def test_negative_tax_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_invoice_totals(
                subtotal=100,
                tax=-10,
            )

    def test_invoice_pdf_is_generated(self):
        invoice_data = {
            "invoice_id": "INV-TEST-001",
            "payment_id": "pay-test-001",
            "status": "succeeded",
            "date": "2026-09-10",
            "currency": "NPR",
            "customer": {
                "name": "Test Customer",
                "email": "test@example.com",
                "phone": "9800000000",
            },
            "billing": {
                "country": "Nepal",
                "city": "Kathmandu",
                "state": "Bagmati",
                "street": "Test Street",
                "zipcode": "44600",
            },
            "items": [
                {
                    "name": "Test Product",
                    "description": "Test invoice item",
                    "amount": Decimal("1000"),
                    "tax": Decimal("130"),
                }
            ],
            "subtotal": Decimal("1000"),
            "tax": Decimal("130"),
            "total": Decimal("1130"),
        }

        pdf_bytes = generate_invoice_pdf(invoice_data)

        self.assertIsInstance(pdf_bytes, bytes)
        self.assertGreater(len(pdf_bytes), 100)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_payment_profile_company_details_are_included(self):
        payment = SimpleNamespace(
            currency="NPR",
            tax=0,
            total_amount=100000,
            customer=SimpleNamespace(
                name="Test Customer",
                email="test@example.com",
                phone_number="9800000000",
            ),
            billing=SimpleNamespace(
                country="Nepal",
                city="Kathmandu",
                state="Bagmati",
                street="Test Street",
                zipcode="44600",
            ),
            invoice_id="INV-TEST-001",
            payment_id="pay-test-001",
            status="succeeded",
            created_at="2026-09-10",
        )

        line_items = SimpleNamespace(items=[])

        payment_profile = SimpleNamespace(
            billing_name="Test Company",
            billing_email="company@example.com",
            phone="9800000000",
            address="Test Address",
            city="Kathmandu",
            state="Bagmati",
            postal_code="44600",
            country="Nepal",
            tax_id="PAN-123",
        )

        result = build_invoice_data(
            payment,
            line_items,
            payment_profile,
        )

        self.assertEqual(
            result["company"]["name"],
            "Test Company",
        )
        self.assertEqual(
            result["company"]["email"],
            "company@example.com",
        )
        self.assertEqual(
            result["company"]["phone"],
            "9800000000",
        )
        self.assertEqual(
            result["company"]["tax_id"],
            "PAN-123",
        )

    def test_minor_to_major_conversion(self):
        result = minor_to_major(
          amount=487162,
          currency="NPR",
        )

        self.assertEqual(result, Decimal("4871.62"))

class MockUser:
    is_authenticated = True
    is_super_admin = True

    def __init__(self, organization_id):
        self.organization_id = organization_id
        self.token_organization_id = organization_id
        self.is_super_admin_claim = True

    def has_permission(self, resource, action):
        return True


class InvoicePDFAPITests(TestCase):

    def setUp(self):
        self.client = APIClient()

        self.organization_id = "org-test-001"
        self.user = MockUser(self.organization_id)

        self.client.force_authenticate(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION="Bearer test-token"
        )

        self.payment_id = "pay-test-001"
        self.url = f"/api/billing/invoices/{self.payment_id}/pdf"

        self.subscription = SimpleNamespace(
            dodo_customer_id="cus-test-001"
        )

        self.payment = SimpleNamespace(
            payment_id=self.payment_id,
            invoice_id="",
            status="succeeded",
            customer=SimpleNamespace(
                customer_id="cus-test-001",
                name="Test Customer",
                email="test@example.com",
                phone_number="9800000000",
            ),
        )

        self.line_items = SimpleNamespace(items=[])

    @patch("apps.billing.views.PaymentProfile.objects.filter")
    @patch("apps.billing.views.generate_invoice_pdf")
    @patch("apps.billing.views.build_invoice_data")
    @patch("apps.billing.views.dodo.get_payment_details")
    @patch("apps.billing.views.get_or_create_subscription")
    def test_invoice_pdf_success(
      self,
      mock_get_subscription,
      mock_get_payment_details,
      mock_build_invoice_data,
      mock_generate_pdf,
      mock_payment_profile,
    ):
        mock_payment_profile.return_value.first.return_value = SimpleNamespace(
          billing_name="Test Company",
          billing_email="company@example.com",
          phone="9800000000",
          address="Test Address",
          city="Kathmandu",
          state="Bagmati",
          postal_code="44600",
          country="Nepal",
          tax_id="",
        )


        mock_get_subscription.return_value = self.subscription

        mock_get_payment_details.return_value = {
            "payment": self.payment,
            "line_items": self.line_items,
        }

        mock_build_invoice_data.return_value = {
            "invoice_id": "INV-TEST-001",
            "payment_id": self.payment_id,
        }

        mock_generate_pdf.return_value = b"%PDF-test-invoice"

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/pdf",
        )
        self.assertTrue(response.content.startswith(b"%PDF"))

        self.assertIn(
            "invoice-INV-TEST-001.pdf",
            response["Content-Disposition"],
        )

    @patch("apps.billing.views.get_or_create_subscription")
    def test_invoice_pdf_no_dodo_customer(
        self,
        mock_get_subscription,
    ):
        mock_get_subscription.return_value = SimpleNamespace(
            dodo_customer_id=""
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["status"], "error")
        self.assertEqual(
            response.data["message"],
            "No Dodo customer on this organization",
        )

    @patch("apps.billing.views.dodo.get_payment_details")
    @patch("apps.billing.views.get_or_create_subscription")
    def test_invoice_pdf_wrong_organization(
        self,
        mock_get_subscription,
        mock_get_payment_details,
    ):
        mock_get_subscription.return_value = self.subscription

        wrong_payment = SimpleNamespace(
            payment_id=self.payment_id,
            status="succeeded",
            customer=SimpleNamespace(
                customer_id="different-customer",
            ),
        )

        mock_get_payment_details.return_value = {
            "payment": wrong_payment,
            "line_items": self.line_items,
        }

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["status"], "error")
        self.assertEqual(
            response.data["message"],
            "Payment does not belong to this organization",
        )

    @patch("apps.billing.views.dodo.get_payment_details")
    @patch("apps.billing.views.get_or_create_subscription")
    def test_invoice_pdf_payment_not_succeeded(
        self,
        mock_get_subscription,
        mock_get_payment_details,
    ):
        mock_get_subscription.return_value = self.subscription

        failed_payment = SimpleNamespace(
            payment_id=self.payment_id,
            status="failed",
            customer=SimpleNamespace(
                customer_id="cus-test-001",
            ),
        )

        mock_get_payment_details.return_value = {
            "payment": failed_payment,
            "line_items": self.line_items,
        }

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["status"], "error")
        self.assertEqual(
            response.data["message"],
            "Invoice is only available for successful payments",
        )