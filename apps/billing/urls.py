from django.urls import path

from . import views

urlpatterns = [
    path("plans", views.list_plans),
    path("plans/sync-dodo", views.sync_dodo_product, name="billing-sync-dodo"),
    path("subscription", views.get_subscription),
    path("checkout", views.create_checkout),
    path("change-plan", views.change_plan),
    path("seats", views.update_seats),
    path("portal", views.customer_portal),
    path("usage", views.usage),
    path("invoices", views.invoices),
    path("invoices/<str:payment_id>/pdf", views.invoice_pdf, name="billing-invoice-pdf"),
    path("payment-profile", views.get_payment_profile),
    path("payment-profile/create", views.create_payment_profile),
    path("payment-profile/update", views.update_payment_profile),
    path("subscription/cancel", views.cancel_subscription, name="billing-cancel-subscription"),
]

