from django.urls import path

from . import views

urlpatterns = [
    path("catalogs/sync", views.sync_catalogs),
    path("catalogs/<uuid:catalog_id>/products", views.catalog_products),
    path("catalogs/<uuid:catalog_id>", views.catalog_detail),
    path("catalogs", views.catalogs_collection),
    path("products/<uuid:product_id>", views.product_detail),
]
