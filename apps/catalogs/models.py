from django.db import models

from apps.accounts.models import Organization, UUIDModel, ActiveManager


class Catalog(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    whatsapp_account = models.CharField(max_length=100, blank=True, db_column="whats_app_account")
    meta_catalog_id = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "catalogs"
        managed = False


class CatalogProduct(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    catalog = models.ForeignKey(Catalog, on_delete=models.DO_NOTHING, db_constraint=False, related_name="products")
    meta_product_id = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    price = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    url = models.CharField(max_length=500, blank=True)
    image_url = models.CharField(max_length=500, blank=True)
    retailer_id = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "catalog_products"
        managed = False
