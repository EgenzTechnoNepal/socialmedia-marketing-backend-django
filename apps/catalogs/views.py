from rest_framework.decorators import api_view, permission_classes

from apps.catalogs.models import Catalog, CatalogProduct
from apps.common.envelope import error, success
from apps.common.exceptions import APIError
from apps.common.http import iso, org_id, request_body, require_perm, soft_delete
from apps.common.permissions import CookieAuthenticated
from apps.whatsapp.models import resolve_account
from services import whatsapp_client
from services.whatsapp_client import WhatsAppError


def _fmt(dt) -> str:
    if dt is None:
        return ""
    value = iso(dt) or ""
    return value.replace("Z", "").replace("+00:00", "") + ("Z" if value else "")


def catalog_payload(catalog: Catalog, product_count: int, products=None) -> dict:
    payload = {
        "id": str(catalog.id),
        "meta_catalog_id": catalog.meta_catalog_id or "",
        "whatsapp_account": catalog.whatsapp_account or "",
        "name": catalog.name,
        "is_active": bool(catalog.is_active),
        "product_count": product_count,
        "created_at": iso(catalog.created_at).replace("+00:00", "Z") if catalog.created_at else "",
        "updated_at": iso(catalog.updated_at).replace("+00:00", "Z") if catalog.updated_at else "",
    }
    if products is not None:
        payload["products"] = [product_payload(p) for p in products]
    return payload


def product_payload(product: CatalogProduct) -> dict:
    return {
        "id": str(product.id),
        "meta_product_id": product.meta_product_id or "",
        "name": product.name,
        "description": product.description or "",
        "price": int(product.price or 0),
        "currency": product.currency or "USD",
        "url": product.url or "",
        "image_url": product.image_url or "",
        "retailer_id": product.retailer_id or "",
        "is_active": bool(product.is_active),
        "created_at": iso(product.created_at).replace("+00:00", "Z") if product.created_at else "",
        "updated_at": iso(product.updated_at).replace("+00:00", "Z") if product.updated_at else "",
    }


def _get_catalog(oid, catalog_id) -> Catalog:
    catalog = Catalog.objects.filter(id=catalog_id, organization_id=oid).first()
    if not catalog:
        raise APIError("Catalog not found", status_code=404)
    return catalog


def _get_product(oid, product_id) -> CatalogProduct:
    product = CatalogProduct.objects.filter(id=product_id, organization_id=oid).first()
    if not product:
        raise APIError("Product not found", status_code=404)
    return product


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def catalogs_collection(request):
    oid = org_id(request)
    require_perm(request, "accounts", "read" if request.method == "GET" else "write")
    if request.method == "GET":
        qs = Catalog.objects.filter(organization_id=oid)
        account = request.query_params.get("whatsapp_account") or ""
        if account:
            qs = qs.filter(whatsapp_account=account)
        result = []
        for catalog in qs.order_by("name"):
            count = CatalogProduct.objects.filter(catalog=catalog).count()
            result.append(catalog_payload(catalog, count))
        return success({"catalogs": result})

    data = request_body(request)
    name = (data.get("name") or "").strip()
    account_name = (data.get("whatsapp_account") or "").strip()
    if not name or not account_name:
        return error("name and whatsapp_account are required", http_status=400)
    account = resolve_account(oid, account_name)
    try:
        meta_id = whatsapp_client.create_catalog(account, name)
    except WhatsAppError:
        return error("Failed to create catalog", http_status=500)
    catalog = Catalog.objects.create(
        organization_id=oid,
        whatsapp_account=account_name,
        meta_catalog_id=meta_id,
        name=name,
        is_active=True,
    )
    return success(catalog_payload(catalog, 0))


@api_view(["GET", "DELETE"])
@permission_classes([CookieAuthenticated])
def catalog_detail(request, catalog_id):
    oid = org_id(request)
    catalog = _get_catalog(oid, catalog_id)
    if request.method == "GET":
        require_perm(request, "accounts", "read")
        products = list(CatalogProduct.objects.filter(catalog=catalog).order_by("name"))
        return success(catalog_payload(catalog, len(products), products))
    require_perm(request, "accounts", "write")
    try:
        account = resolve_account(oid, catalog.whatsapp_account)
        if catalog.meta_catalog_id:
            whatsapp_client.delete_catalog(account, catalog.meta_catalog_id)
    except (APIError, WhatsAppError):
        pass
    from django.utils import timezone as dj_tz

    now = dj_tz.now()
    CatalogProduct.objects.filter(catalog=catalog).update(deleted_at=now, updated_at=now)
    soft_delete(catalog)
    return success({"message": "Catalog deleted"})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def sync_catalogs(request):
    oid = org_id(request)
    require_perm(request, "accounts", "write")
    account_name = (request_body(request).get("whatsapp_account") or "").strip()
    if not account_name:
        return error("whatsapp_account is required", http_status=400)
    account = resolve_account(oid, account_name)
    try:
        meta_catalogs = whatsapp_client.list_catalogs(account)
    except WhatsAppError:
        return error("Failed to fetch catalogs", http_status=500)
    synced = 0
    for mc in meta_catalogs:
        meta_id = mc.get("id") or ""
        if not meta_id:
            continue
        existing = Catalog.objects.filter(organization_id=oid, meta_catalog_id=meta_id).first()
        if existing is None:
            Catalog.objects.create(
                organization_id=oid,
                whatsapp_account=account_name,
                meta_catalog_id=meta_id,
                name=mc.get("name") or "",
                is_active=True,
            )
        else:
            existing.name = mc.get("name") or existing.name
            existing.save(update_fields=["name", "updated_at"])
        synced += 1
    return success({"message": "Catalogs synced", "synced": synced, "total": len(meta_catalogs)})


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def catalog_products(request, catalog_id):
    oid = org_id(request)
    catalog = _get_catalog(oid, catalog_id)
    if request.method == "GET":
        require_perm(request, "accounts", "read")
        products = CatalogProduct.objects.filter(catalog=catalog).order_by("name")
        return success({"products": [product_payload(p) for p in products]})
    require_perm(request, "accounts", "write")
    data = request_body(request)
    name = (data.get("name") or "").strip()
    try:
        price = int(data.get("price") or 0)
    except (TypeError, ValueError):
        price = 0
    if not name or price <= 0:
        return error("name and price are required", http_status=400)
    account = resolve_account(oid, catalog.whatsapp_account)
    currency = data.get("currency") or "USD"
    product_in = {
        "name": name,
        "price": price,
        "currency": currency,
        "url": data.get("url") or "",
        "image_url": data.get("image_url") or "",
        "retailer_id": data.get("retailer_id") or "",
        "description": data.get("description") or "",
    }
    try:
        meta_id = whatsapp_client.create_product(account, catalog.meta_catalog_id, product_in)
    except WhatsAppError:
        return error("Failed to create product", http_status=500)
    product = CatalogProduct.objects.create(
        organization_id=oid,
        catalog=catalog,
        meta_product_id=meta_id,
        name=name,
        description=product_in["description"],
        price=price,
        currency=currency,
        url=product_in["url"],
        image_url=product_in["image_url"],
        retailer_id=product_in["retailer_id"],
        is_active=True,
    )
    return success(product_payload(product))


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def product_detail(request, product_id):
    oid = org_id(request)
    product = _get_product(oid, product_id)
    if request.method == "GET":
        require_perm(request, "accounts", "read")
        return success(product_payload(product))
    catalog = Catalog.objects.filter(id=product.catalog_id).first()
    if not catalog:
        return error("Catalog not found", http_status=404)
    if request.method == "DELETE":
        require_perm(request, "accounts", "write")
        try:
            account = resolve_account(oid, catalog.whatsapp_account)
            if product.meta_product_id:
                whatsapp_client.delete_product(account, product.meta_product_id)
        except (APIError, WhatsAppError):
            pass
        soft_delete(product)
        return success({"message": "Product deleted"})
    require_perm(request, "accounts", "write")
    data = request_body(request)
    account = resolve_account(oid, catalog.whatsapp_account)
    try:
        whatsapp_client.update_product(
            account,
            product.meta_product_id,
            {
                "name": data.get("name") or "",
                "price": data.get("price") or 0,
                "currency": data.get("currency") or "",
                "url": data.get("url") or "",
                "image_url": data.get("image_url") or "",
                "description": data.get("description") or "",
            },
        )
    except WhatsAppError:
        return error("Failed to update product", http_status=500)
    if data.get("name"):
        product.name = data["name"]
    if data.get("description"):
        product.description = data["description"]
    if data.get("price"):
        product.price = int(data["price"])
    if data.get("currency"):
        product.currency = data["currency"]
    if data.get("url"):
        product.url = data["url"]
    if data.get("image_url"):
        product.image_url = data["image_url"]
    if data.get("retailer_id"):
        product.retailer_id = data["retailer_id"]
    product.save()
    return success(product_payload(product))
