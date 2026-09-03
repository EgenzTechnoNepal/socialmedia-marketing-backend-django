from django.urls import path

from . import views

urlpatterns = [
    path("plans", views.list_plans),
    path("subscription", views.get_subscription),
    path("checkout", views.create_checkout),
    path("change-plan", views.change_plan),
    path("seats", views.update_seats),
    path("portal", views.customer_portal),
    path("usage", views.usage),
    path("invoices", views.invoices),
]
