from rest_framework.decorators import api_view, permission_classes

from apps.billing.entitlements import FEATURE_EXTRA_WA, assert_feature, get_or_create_subscription
from apps.common.envelope import error, success
from apps.common.http import iso, org_id, require_perm
from apps.common.permissions import CookieAuthenticated
from apps.whatsapp.models import WhatsAppAccount


def _account_payload(account: WhatsAppAccount) -> dict:
    return {
        "id": str(account.id),
        "name": account.name,
        "app_id": account.app_id or "",
        "phone_id": account.phone_id,
        "business_id": account.business_id,
        "webhook_verify_token": account.webhook_verify_token or "",
        "api_version": account.api_version or "v21.0",
        "is_default_incoming": account.is_default_incoming,
        "is_default_outgoing": account.is_default_outgoing,
        "auto_read_receipt": account.auto_read_receipt,
        "business_calling_enabled": account.business_calling_enabled,
        "is_smb": account.is_smb,
        "status": account.status,
        "has_access_token": bool(account.access_token),
        "has_app_secret": bool(account.app_secret),
        "created_at": iso(account.created_at),
        "updated_at": iso(account.updated_at),
    }


def _clear_defaults(oid, incoming=False, outgoing=False):
    qs = WhatsAppAccount.objects.filter(organization_id=oid)
    if incoming:
        qs.filter(is_default_incoming=True).update(is_default_incoming=False)
    if outgoing:
        qs.filter(is_default_outgoing=True).update(is_default_outgoing=False)


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def accounts_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        require_perm(request, "accounts", "read")
        accounts = WhatsAppAccount.objects.filter(organization_id=oid).order_by("name")
        return success({"accounts": [_account_payload(a) for a in accounts]})

    require_perm(request, "accounts", "write")
    existing = WhatsAppAccount.objects.filter(organization_id=oid).count()
    get_or_create_subscription(oid)
    if existing >= 1:
        assert_feature(oid, FEATURE_EXTRA_WA)
    data = request.data if isinstance(request.data, dict) else {}
    name = (data.get("name") or "").strip()
    phone_id = (data.get("phone_id") or "").strip()
    business_id = (data.get("business_id") or "").strip()
    access_token = data.get("access_token") or ""
    if not name or not phone_id or not business_id or not access_token:
        return error("name, phone_id, business_id, and access_token are required", http_status=400)
    if data.get("is_default_incoming"):
        _clear_defaults(oid, incoming=True)
    if data.get("is_default_outgoing"):
        _clear_defaults(oid, outgoing=True)
    account = WhatsAppAccount(
        organization_id=oid,
        name=name,
        app_id=data.get("app_id") or "",
        phone_id=phone_id,
        business_id=business_id,
        access_token=access_token,
        app_secret=data.get("app_secret") or "",
        webhook_verify_token=data.get("webhook_verify_token") or "",
        api_version=data.get("api_version") or "v21.0",
        is_default_incoming=bool(data.get("is_default_incoming")),
        is_default_outgoing=bool(data.get("is_default_outgoing")),
        auto_read_receipt=bool(data.get("auto_read_receipt")),
        business_calling_enabled=bool(data.get("business_calling_enabled")),
        created_by=request.user,
        updated_by=request.user,
    )
    account.encrypt_secrets()
    account.save()
    return success(_account_payload(account), http_status=201)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def account_detail(request, account_id):
    oid = org_id(request)
    try:
        account = WhatsAppAccount.objects.get(id=account_id, organization_id=oid)
    except WhatsAppAccount.DoesNotExist:
        return error("Account not found", http_status=404)
    if request.method == "GET":
        require_perm(request, "accounts", "read")
        return success(_account_payload(account))
    if request.method == "DELETE":
        require_perm(request, "accounts", "delete")
        from django.utils import timezone as dj_tz

        account.deleted_at = dj_tz.now()
        account.save(update_fields=["deleted_at"])
        return success({"message": "Account deleted"})
    require_perm(request, "accounts", "write")
    data = request.data if isinstance(request.data, dict) else {}
    for field in ("name", "app_id", "phone_id", "business_id", "webhook_verify_token", "api_version"):
        if field in data and data.get(field) is not None:
            setattr(account, field, data.get(field) or "")
    for flag in ("is_default_incoming", "is_default_outgoing", "auto_read_receipt", "business_calling_enabled"):
        if flag in data:
            setattr(account, flag, bool(data[flag]))
    if data.get("is_default_incoming"):
        _clear_defaults(oid, incoming=True)
        account.is_default_incoming = True
    if data.get("is_default_outgoing"):
        _clear_defaults(oid, outgoing=True)
        account.is_default_outgoing = True
    if data.get("access_token"):
        account.access_token = data["access_token"]
    if "app_secret" in data and data.get("app_secret"):
        account.app_secret = data["app_secret"]
    account.updated_by = request.user
    account.encrypt_secrets()
    account.save()
    return success(_account_payload(account))
