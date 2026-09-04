from django.conf import settings
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser

from apps.accounts.models import Organization
from apps.billing.entitlements import FEATURE_EXTRA_WA, assert_feature, get_or_create_subscription
from apps.common.envelope import error, success
from apps.common.http import iso, org_id, require_perm
from apps.common.permissions import CookieAuthenticated
from apps.whatsapp.models import WhatsAppAccount
from services.crypto import decrypt
from services.whatsapp_client import WhatsAppError
from services import whatsapp_client


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


def _get_account(request, account_id) -> WhatsAppAccount | None:
    oid = org_id(request)
    account = WhatsAppAccount.objects.filter(id=account_id, organization_id=oid).first()
    if account:
        account.decrypt_secrets()
    return account


def _resolve_meta_creds(oid):
    app_id = settings.META_APP_ID
    app_secret = settings.META_APP_SECRET
    config_id = settings.META_CONFIG_ID
    org = Organization.objects.filter(id=oid).first()
    settings_json = (org.settings or {}) if org else {}
    if settings_json.get("meta_app_id"):
        app_id = settings_json["meta_app_id"]
    if settings_json.get("meta_config_id"):
        config_id = settings_json["meta_config_id"]
    encrypted = settings_json.get("meta_app_secret_encrypted") or ""
    if encrypted:
        app_secret = decrypt(encrypted, settings.ENCRYPTION_KEY) or app_secret
    return app_id, app_secret, config_id


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def embedded_signup_config(request):
    oid = org_id(request)
    require_perm(request, "accounts", "read")
    app_id, _, config_id = _resolve_meta_creds(oid)
    return success(
        {
            "whatsapp_app_id": app_id or "",
            "whatsapp_config_id": config_id or "",
            "whatsapp_api_version": settings.WHATSAPP_API_VERSION,
        }
    )


def _discover_waba_and_phone(code_token: str, app_id: str, app_secret: str, phone_id: str, waba_id: str, name: str, api_version: str):
    if phone_id and waba_id:
        return phone_id, waba_id, name
    if not app_id or not app_secret:
        raise WhatsAppError("phone_id and waba_id are required when Meta app credentials are missing")
    debug = whatsapp_client.debug_token(code_token, f"{app_id}|{app_secret}")
    discovered = ""
    for scope in debug.get("granular_scopes") or []:
        if scope.get("scope") == "whatsapp_business_management" and scope.get("target_ids"):
            discovered = scope["target_ids"][0]
            break
    if not discovered:
        shared = whatsapp_client.shared_waba(code_token)
        if shared:
            discovered = shared[0].get("id") or ""
    if not discovered:
        raise WhatsAppError("could not discover WhatsApp Business Account ID from token")
    waba_id = waba_id or discovered
    if not phone_id:
        phones = whatsapp_client.waba_phone_numbers(waba_id, code_token, api_version)
        if not phones:
            raise WhatsAppError("no phone numbers found in this WhatsApp Business Account")
        phone = phones[0]
        phone_id = phone.get("id") or ""
        name = name or f"{phone.get('verified_name') or ''} ({phone.get('display_phone_number') or ''})".strip()
    return phone_id, waba_id, name


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def exchange_token(request):
    oid = org_id(request)
    require_perm(request, "accounts", "write")
    data = request.data if isinstance(request.data, dict) else {}
    code = (data.get("code") or "").strip()
    if not code:
        return error("Code is required", http_status=400)
    app_id, app_secret, _ = _resolve_meta_creds(oid)
    if not app_id or not app_secret:
        return error("Meta app credentials are not configured for this organization", http_status=400)
    api_version = settings.WHATSAPP_API_VERSION
    try:
        access_token = whatsapp_client.exchange_code_for_token(code, app_id, app_secret, api_version)
        phone_id, waba_id, name = _discover_waba_and_phone(
            access_token,
            app_id,
            app_secret,
            (data.get("phone_id") or "").strip(),
            (data.get("waba_id") or "").strip(),
            (data.get("name") or "").strip(),
            api_version,
        )
    except WhatsAppError as exc:
        return error(str(exc), http_status=400)
    if not phone_id or not waba_id:
        return error("Could not resolve phone_id and waba_id", http_status=400)
    existing = WhatsAppAccount.all_objects.filter(organization_id=oid, phone_id=phone_id).first()
    get_or_create_subscription(oid)
    if existing is None and WhatsAppAccount.objects.filter(organization_id=oid).count() >= 1:
        assert_feature(oid, FEATURE_EXTRA_WA)
    if not name:
        try:
            info = whatsapp_client.phone_number_info(phone_id, access_token, api_version)
            name = f"{info.get('verified_name') or 'WhatsApp'} ({info.get('display_phone_number') or phone_id})"
        except WhatsAppError:
            name = f"WhatsApp {phone_id[-6:]}"
    if existing:
        account = existing
        account.deleted_at = None
        account.access_token = access_token
        account.business_id = waba_id
        account.app_id = app_id
        account.app_secret = app_secret
        account.name = name or account.name
        if data.get("webhook_verify_token"):
            account.webhook_verify_token = data["webhook_verify_token"]
        account.status = "active"
        account.updated_by = request.user
    else:
        account = WhatsAppAccount(
            organization_id=oid,
            name=name,
            app_id=app_id,
            phone_id=phone_id,
            business_id=waba_id,
            access_token=access_token,
            app_secret=app_secret,
            webhook_verify_token=data.get("webhook_verify_token") or "",
            api_version=api_version,
            status="active",
            created_by=request.user,
            updated_by=request.user,
        )
    account.encrypt_secrets()
    account.save()
    account.decrypt_secrets()
    warning = ""
    try:
        whatsapp_client.subscribe_waba(account)
    except WhatsAppError as exc:
        warning = str(exc)
    out = {"account": _account_payload(account)}
    if warning:
        out["warning"] = warning
    return success(out)


@api_view(["GET", "PUT"])
@permission_classes([CookieAuthenticated])
def business_profile(request, account_id):
    account = _get_account(request, account_id)
    if account is None:
        return error("Account not found", http_status=404)
    try:
        if request.method == "GET":
            require_perm(request, "accounts", "read")
            return success(whatsapp_client.get_business_profile(account))
        require_perm(request, "accounts", "write")
        data = request.data if isinstance(request.data, dict) else {}
        data.setdefault("messaging_product", "whatsapp")
        profile = whatsapp_client.update_business_profile(account, data)
        return success(profile)
    except WhatsAppError as exc:
        return error("Failed to update business profile" if request.method == "PUT" else "Failed to get business profile", http_status=500, extra={"detail": str(exc)})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def business_profile_photo(request, account_id):
    require_perm(request, "accounts", "write")
    account = _get_account(request, account_id)
    if account is None:
        return error("Account not found", http_status=404)
    upload = request.FILES.get("file")
    if not upload:
        return error("Missing file", http_status=400)
    try:
        handle = whatsapp_client.upload_profile_picture(account, upload.read(), upload.content_type or "image/jpeg")
    except WhatsAppError as exc:
        return error("Failed to upload profile picture", http_status=500, extra={"detail": str(exc)})
    return success({"message": "Profile picture updated successfully", "handle": handle})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def test_account(request, account_id):
    require_perm(request, "accounts", "read")
    account = _get_account(request, account_id)
    if account is None:
        return error("Account not found", http_status=404)
    try:
        result = whatsapp_client.test_phone(account)
    except WhatsAppError as exc:
        return success({"success": False, "error": str(exc)})
    account_mode = result.get("account_mode") or ""
    messaging_limit = result.get("messaging_limit_tier") or result.get("whatsapp_business_manager_messaging_limit")
    return success(
        {
            "success": True,
            "display_phone_number": result.get("display_phone_number") or "",
            "verified_name": result.get("verified_name") or "",
            "quality_rating": result.get("quality_rating") or "",
            "account_mode": account_mode,
            "is_test_number": account_mode == "SANDBOX",
            "messaging_limit_tier": messaging_limit,
            "code_verification_status": result.get("code_verification_status") or "",
        }
    )


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def subscribe_account(request, account_id):
    require_perm(request, "accounts", "write")
    account = _get_account(request, account_id)
    if account is None:
        return error("Account not found", http_status=404)
    try:
        whatsapp_client.subscribe_waba(account)
    except WhatsAppError as exc:
        return success({"success": False, "error": "Failed to subscribe app to webhooks. Check your credentials.", "detail": str(exc)})
    return success({"success": True, "message": "App subscribed to webhooks successfully. You should now receive incoming messages."})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def register_phone(request, account_id):
    require_perm(request, "accounts", "write")
    account = _get_account(request, account_id)
    if account is None:
        return error("Account not found", http_status=404)
    data = request.data if isinstance(request.data, dict) else {}
    pin = (data.get("pin") or "").strip()
    if not pin:
        return error("pin is required", http_status=400)
    url = f"https://graph.facebook.com/{account.api_version or settings.WHATSAPP_API_VERSION}/{account.phone_id}/register"
    try:
        whatsapp_client._request("POST", url, account.access_token, {"messaging_product": "whatsapp", "pin": pin})
    except WhatsAppError as exc:
        return error(str(exc), http_status=400)
    account.status = "active"
    account.pin = pin
    account.encrypt_secrets()
    account.save(update_fields=["status", "pin", "updated_at"])
    return success({"success": True, "message": "Phone number registered"})
