from __future__ import annotations
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from decimal import Decimal, InvalidOperation

CURRENCY_DECIMALS = {
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "PYG": 0,
    "RWF": 0,
    "UGX": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,

    "BHD": 3,
    "JOD": 3,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
}


def minor_to_major(
    amount: int | float | Decimal,
    currency: str,
) -> Decimal:
    """
    Convert a Dodo smallest-unit amount to the currency's
    major unit for display and invoice calculations.
    """

    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid currency amount") from exc

    if amount < 0:
        raise ValueError("Currency amount cannot be negative")

    decimals = CURRENCY_DECIMALS.get(currency.upper(), 2)

    return amount / (Decimal("10") ** decimals)


def calculate_invoice_totals(
    subtotal: int | float | Decimal,
    tax: int | float | Decimal = 0,
) -> dict[str, Decimal]:
    """
    Calculate invoice subtotal, tax, and total.

    Amounts are expected to be in the currency's major unit
    (for example, 100.50 NPR rather than 10050 paisa).
    """
    try:
        subtotal = Decimal(str(subtotal))
        tax = Decimal(str(tax))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid subtotal or tax amount") from exc

    if subtotal < 0:
        raise ValueError("Subtotal cannot be negative")

    if tax < 0:
        raise ValueError("Tax cannot be negative")

    total = subtotal + tax

    return {
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
    }


def build_invoice_data(
    payment,
    line_items,
    payment_profile=None,
) -> dict:
    """
    Convert a Dodo payment and line-item response into
    normalized invoice data.

    Financial totals use the customer's payment currency
    and payment-level amounts from Dodo.
    """

    currency = payment.currency

    raw_payment_tax = payment.tax or 0
    raw_total_amount = payment.total_amount

    if raw_total_amount is None:
        raise ValueError("Payment total amount is missing")

    payment_tax = minor_to_major(
        raw_payment_tax,
        currency,
    )

    total_amount = minor_to_major(
        raw_total_amount,
        currency,
    )

    if payment_tax < 0:
        raise ValueError("Payment tax cannot be negative")

    subtotal_amount = total_amount - payment_tax

    if subtotal_amount < 0:
        raise ValueError(
            "Payment tax cannot be greater than total amount"
        )

    customer = payment.customer
    billing = payment.billing

    items = list(line_items.items or [])

    invoice_items = []

    if len(items) == 1:
        item = items[0]

        invoice_items.append(
            {
                "name": item.name or "Item",
                "description": item.description or "",
                "amount": subtotal_amount,
                "tax": payment_tax,
            }
        )
    else:
        for item in items:
            invoice_items.append(
                {
                    "name": item.name or "Item",
                    "description": item.description or "",
                    "amount": item.amount,
                    "tax": item.tax,
                }
            )

    company = {
        "name": "",
        "email": "",
        "phone": "",
        "address": "",
        "city": "",
        "state": "",
        "postal_code": "",
        "country": "",
        "tax_id": "",
    }

    if payment_profile:
        company = {
            "name": payment_profile.billing_name or "",
            "email": payment_profile.billing_email or "",
            "phone": payment_profile.phone or "",
            "address": payment_profile.address or "",
            "city": payment_profile.city or "",
            "state": payment_profile.state or "",
            "postal_code": payment_profile.postal_code or "",
            "country": payment_profile.country or "",
            "tax_id": payment_profile.tax_id or "",
        }

    return {
        "invoice_id": payment.invoice_id or "",
        "payment_id": payment.payment_id,
        "status": payment.status,
        "date": payment.created_at,
        "currency": currency,

        "company": company,

        "customer": {
            "name": customer.name or "",
            "email": customer.email or "",
            "phone": customer.phone_number or "",
        },

        "billing": {
            "country": billing.country or "",
            "city": billing.city or "",
            "state": billing.state or "",
            "street": billing.street or "",
            "zipcode": billing.zipcode or "",
        },

        "items": invoice_items,

        "subtotal": subtotal_amount,
        "tax": payment_tax,
        "total": total_amount,
    }


def generate_invoice_pdf(invoice_data: dict) -> bytes:
    """
    Generate a professional PDF invoice from normalized invoice data.
    """

    required_fields = [
        "currency",
        "customer",
        "items",
        "subtotal",
        "tax",
        "total",
    ]

    for field in required_fields:
        if field not in invoice_data:
            raise ValueError(f"Missing invoice field: {field}")

    buffer = BytesIO()

    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "InvoiceTitle",
        parent=styles["Title"],
        fontSize=22,
        spaceAfter=10,
    )

    heading_style = ParagraphStyle(
        "InvoiceHeading",
        parent=styles["Heading2"],
        fontSize=11,
        spaceAfter=6,
    )

    normal_style = ParagraphStyle(
        "InvoiceNormal",
        parent=styles["Normal"],
        fontSize=9,
        leading=13,
    )

    right_style = ParagraphStyle(
        "InvoiceRight",
        parent=normal_style,
        alignment=TA_RIGHT,
    )

    story = []

    # Header
    story.append(Paragraph("INVOICE", title_style))

    invoice_number = invoice_data.get("invoice_id") or invoice_data.get(
        "payment_id",
        "",
    )

    story.append(
        Paragraph(
            f"<b>Invoice Number:</b> {invoice_number}",
            normal_style,
        )
    )

    invoice_date = invoice_data.get("date")

    if invoice_date:
        story.append(
            Paragraph(
                f"<b>Date:</b> {invoice_date}",
                normal_style,
            )
        )

    story.append(Spacer(1, 10))

    # Company information
    company = invoice_data.get("company", {})

    story.append(Paragraph("From", heading_style))

    company_name = company.get("name") or "Company"

    company_lines = [
        f"<b>{company_name}</b>",
    ]

    if company.get("email"):
        company_lines.append(
            f"Email: {company['email']}"
        )

    if company.get("phone"):
        company_lines.append(
            f"Phone: {company['phone']}"
        )

    if company.get("address"):
        company_lines.append(
            company["address"]
        )

    location_parts = [
        company.get("city"),
        company.get("state"),
        company.get("postal_code"),
        company.get("country"),
    ]

    location = ", ".join(
        str(part)
        for part in location_parts
        if part
    )

    if location:
        company_lines.append(location)

    if company.get("tax_id"):
        company_lines.append(
            f"Tax ID: {company['tax_id']}"
        )

    story.append(
        Paragraph(
            "<br/>".join(company_lines),
            normal_style,
        )
    )

    story.append(Spacer(1, 10))

    # Customer information
    customer = invoice_data.get("customer", {})
    billing = invoice_data.get("billing", {})

    story.append(Paragraph("Bill To", heading_style))

    customer_lines = []

    if customer.get("name"):
        customer_lines.append(
            f"<b>{customer['name']}</b>"
        )

    if customer.get("email"):
        customer_lines.append(
            customer["email"]
        )

    if customer.get("phone"):
        customer_lines.append(
            customer["phone"]
        )

    billing_street = billing.get("street")

    if billing_street:
        customer_lines.append(billing_street)

    billing_location_parts = [
        billing.get("city"),
        billing.get("state"),
        billing.get("zipcode"),
        billing.get("country"),
    ]

    billing_location = ", ".join(
        str(part)
        for part in billing_location_parts
        if part
    )

    if billing_location:
        customer_lines.append(billing_location)

    story.append(
        Paragraph(
            "<br/>".join(customer_lines),
            normal_style,
        )
    )

    story.append(Spacer(1, 15))

    # Items
    currency = invoice_data["currency"]

    table_data = [
        [
            Paragraph("<b>Item</b>", normal_style),
            Paragraph("<b>Description</b>", normal_style),
            Paragraph("<b>Amount</b>", right_style),
        ]
    ]

    for item in invoice_data["items"]:
        table_data.append(
            [
                Paragraph(
                    item.get("name", "Item"),
                    normal_style,
                ),
                Paragraph(
                    item.get("description", ""),
                    normal_style,
                ),
                Paragraph(
                    f"{currency} {item.get('amount', 0):,.2f}",
                    right_style,
                ),
            ]
        )

    item_table = Table(
        table_data,
        colWidths=[55 * mm, 75 * mm, 40 * mm],
        repeatRows=1,
    )

    item_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    story.append(item_table)

    story.append(Spacer(1, 15))

    # Financial summary
    totals_data = [
        [
            Paragraph("<b>Subtotal</b>", normal_style),
            Paragraph(
                f"{currency} {invoice_data['subtotal']:,.2f}",
                right_style,
            ),
        ],
        [
            Paragraph("<b>Tax / VAT</b>", normal_style),
            Paragraph(
                f"{currency} {invoice_data['tax']:,.2f}",
                right_style,
            ),
        ],
        [
            Paragraph("<b>Total</b>", normal_style),
            Paragraph(
                f"<b>{currency} {invoice_data['total']:,.2f}</b>",
                right_style,
            ),
        ],
    ]

    totals_table = Table(
        totals_data,
        colWidths=[130 * mm, 40 * mm],
    )

    totals_table.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, -1), 0.5, colors.grey),
                ("LINEBELOW", (0, -1), (-1, -1), 1, colors.black),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    story.append(totals_table)

    story.append(Spacer(1, 15))

    # Payment reference
    story.append(
        Paragraph(
            f"<b>Payment Reference:</b> "
            f"{invoice_data.get('payment_id', '')}",
            normal_style,
        )
    )

    story.append(Spacer(1, 20))

    story.append(
        Paragraph(
            "Thank you for your business.",
            normal_style,
        )
    )

    document.build(story)

    return buffer.getvalue()