import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from django.conf import settings
from django.db import DatabaseError
from django.http import HttpResponseRedirect
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny

from apps.accounts.models import CustomRole, SSOProvider, User, UserOrganization
from apps.accounts.payloads import apply_org_role
from apps.common.cookies import set_auth_cookies
from apps.common.envelope import error, success
from apps.common.http import org_id, request_body, require_perm
from apps.common.permissions import CookieAuthenticated
from apps.common.tokens import generate_access_token, generate_refresh_token, redis_client
from services.crypto import decrypt, encrypt

OAUTH = {
    "google": {
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "userinfo": "https://www.googleapis.com/oauth2/v2/userinfo",
        "scopes": ["openid", "email", "profile"],
    },
    "microsoft": {
        "auth": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "userinfo": "https://graph.microsoft.com/v1.0/me",
        "scopes": ["openid", "email", "profile", "User.Read"],
    },
    "github": {
        "auth": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "userinfo": "https://api.github.com/user",
        "scopes": ["user:email", "read:user"],
    },
    "facebook": {
        "auth": "https://www.facebook.com/v18.0/dialog/oauth",
        "token": "https://graph.facebook.com/v18.0/oauth/access_token",
        "userinfo": "https://graph.facebook.com/me?fields=id,email,name",
        "scopes": ["email", "public_profile"],
    },
}
DISPLAY_NAMES = {
    "google": "Google",
    "microsoft": "Microsoft",
    "github": "GitHub",
    "facebook": "Facebook",
    "custom": "Custom SSO",
}
VALID_PROVIDERS = set(OAUTH) | {"custom"}


def _frontend_base() -> str:
    return (settings.PUBLIC_APP_URL or "http://localhost:3000").rstrip("/")


def _callback_url(provider: str) -> str:
    return f"{_frontend_base()}/api/auth/sso/{provider}/callback"


def _provider_payload(row: SSOProvider) -> dict:
    payload = {
        "provider": row.provider,
        "client_id": row.client_id or "",
        "has_secret": bool(row.client_secret),
        "is_enabled": bool(row.is_enabled),
        "allow_auto_create": bool(row.allow_auto_create),
        "default_role": row.default_role_name or "agent",
        "allowed_domains": row.allowed_domains or "",
    }
    if row.auth_url:
        payload["auth_url"] = row.auth_url
    if row.token_url:
        payload["token_url"] = row.token_url
    if row.user_info_url:
        payload["user_info_url"] = row.user_info_url
    return payload


def _json_get(url: str, token: str, extra_headers=None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _post_form(url: str, data: dict, accept_json=False) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if accept_json:
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8") or ""
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return dict(urllib.parse.parse_qsl(raw))


def _oauth_cfg(provider: str, row: SSOProvider):
    if provider == "custom":
        return {
            "auth": row.auth_url,
            "token": row.token_url,
            "userinfo": row.user_info_url,
            "scopes": ["openid", "email", "profile"],
        }
    return OAUTH[provider]


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def public_providers(request):
    seen = set()
    result = []
    try:
        rows = SSOProvider.objects.filter(is_enabled=True)
        for row in rows:
            if row.provider in seen:
                continue
            seen.add(row.provider)
            result.append({"provider": row.provider, "name": DISPLAY_NAMES.get(row.provider, row.provider)})
    except DatabaseError:
        return success([])
    return success(result)


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def init_sso(request, provider):
    if provider != "custom" and provider not in OAUTH:
        return error("Invalid SSO provider", http_status=400)
    row = SSOProvider.objects.filter(provider=provider, is_enabled=True).first()
    if not row:
        return error("SSO provider not configured or disabled", http_status=404)
    nonce = secrets.token_urlsafe(24)
    state = {
        "org_id": str(row.organization_id),
        "provider": provider,
        "nonce": nonce,
        "expires_at": datetime.now(timezone.utc).timestamp() + 300,
    }
    try:
        redis_client().setex(f"sso:state:{nonce}", 300, json.dumps(state))
    except Exception:
        return error("Failed to initiate SSO", http_status=500)
    cfg = _oauth_cfg(provider, row)
    params = {
        "client_id": row.client_id,
        "redirect_uri": _callback_url(provider),
        "response_type": "code",
        "scope": " ".join(cfg["scopes"]),
        "state": nonce,
        "access_type": "offline",
    }
    return HttpResponseRedirect(f"{cfg['auth']}?{urllib.parse.urlencode(params)}")


def _redirect_error(message: str):
    return HttpResponseRedirect(f"{_frontend_base()}/login?sso_error={urllib.parse.quote(message)}")


def _parse_userinfo(provider: str, raw: dict, token: str) -> tuple[str, str, str]:
    if provider == "google":
        return str(raw.get("id") or ""), raw.get("email") or "", raw.get("name") or ""
    if provider == "microsoft":
        email = raw.get("mail") or raw.get("userPrincipalName") or ""
        return str(raw.get("id") or ""), email, raw.get("displayName") or ""
    if provider == "github":
        email = raw.get("email") or ""
        name = raw.get("name") or raw.get("login") or ""
        if not email:
            try:
                emails = _json_get(
                    "https://api.github.com/user/emails",
                    token,
                    {"Accept": "application/vnd.github+json"},
                )
                if isinstance(emails, list):
                    primary = next((e for e in emails if e.get("primary") and e.get("email")), None)
                    email = (primary or (emails[0] if emails else {})).get("email") or ""
            except Exception:
                pass
        return str(raw.get("id") or ""), email, name
    if provider == "facebook":
        return str(raw.get("id") or ""), raw.get("email") or "", raw.get("name") or ""
    uid = str(raw.get("sub") or raw.get("id") or "")
    name = raw.get("name") or raw.get("preferred_username") or ""
    return uid, raw.get("email") or "", name


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def callback_sso(request, provider):
    if request.query_params.get("error"):
        return _redirect_error("SSO failed: " + (request.query_params.get("error_description") or ""))
    code = request.query_params.get("code") or ""
    nonce = request.query_params.get("state") or ""
    if not code or not nonce:
        return _redirect_error("Invalid callback parameters")
    try:
        raw_state = redis_client().get(f"sso:state:{nonce}")
        redis_client().delete(f"sso:state:{nonce}")
    except Exception:
        raw_state = None
    if not raw_state:
        return _redirect_error("Invalid or expired state")
    try:
        state = json.loads(raw_state)
    except json.JSONDecodeError:
        return _redirect_error("Invalid state")
    if state.get("provider") != provider or datetime.now(timezone.utc).timestamp() > float(state.get("expires_at") or 0):
        return _redirect_error("Invalid or expired state")
    org_uuid = state.get("org_id")
    row = SSOProvider.objects.filter(organization_id=org_uuid, provider=provider).first()
    if not row:
        return _redirect_error("SSO provider not configured")
    secret = decrypt(row.client_secret or "", settings.ENCRYPTION_KEY)
    cfg = _oauth_cfg(provider, row)
    try:
        token_payload = _post_form(
            cfg["token"],
            {
                "client_id": row.client_id,
                "client_secret": secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": _callback_url(provider),
            },
            accept_json=True,
        )
        access = token_payload.get("access_token") or ""
        if not access:
            return _redirect_error("Failed to authenticate with provider")
        headers = {"Accept": "application/vnd.github+json"} if provider == "github" else None
        info = _json_get(cfg["userinfo"], access, headers)
        provider_id, email, name = _parse_userinfo(provider, info, access)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError):
        return _redirect_error("Failed to authenticate with provider")
    if not email:
        return _redirect_error("Failed to get user information")
    if row.allowed_domains:
        domain = email.split("@")[-1].lower().strip()
        allowed = [d.lower().strip() for d in row.allowed_domains.split(",") if d.strip()]
        if domain not in allowed:
            return _redirect_error("Email domain not allowed for this organization")
    user = User.objects.filter(email=email).first()
    if user is None:
        if not row.allow_auto_create:
            return _redirect_error("User not found. Contact your administrator.")
        role_name = row.default_role_name or "agent"
        role = CustomRole.objects.filter(organization_id=org_uuid, name=role_name).first()
        if not role:
            return _redirect_error("Failed to create user account: role not found")
        user = User.objects.create(
            organization_id=org_uuid,
            email=email,
            full_name=name or "",
            password_hash="",
            role_id=role.id,
            is_active=True,
            is_available=True,
            sso_provider=provider,
            sso_provider_id=provider_id,
        )
        UserOrganization.objects.create(
            user=user, organization_id=org_uuid, role_id=role.id, is_default=True
        )
    else:
        if not user.is_active:
            return _redirect_error("Account is disabled")
        if not user.sso_provider:
            user.sso_provider = provider
            user.sso_provider_id = provider_id
            user.save(update_fields=["sso_provider", "sso_provider_id", "updated_at"])
    apply_org_role(user, user.organization_id)
    access = generate_access_token(user, user.organization_id)
    refresh = generate_refresh_token(user, user.organization_id)
    response = HttpResponseRedirect(f"{_frontend_base()}/auth/sso/callback")
    return set_auth_cookies(response, access, refresh)


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def sso_settings(request):
    oid = org_id(request)
    require_perm(request, "settings.sso", "read")
    rows = SSOProvider.objects.filter(organization_id=oid)
    return success([_provider_payload(r) for r in rows])


@api_view(["PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def sso_provider_detail(request, provider):
    oid = org_id(request)
    if provider not in VALID_PROVIDERS:
        return error("Invalid provider", http_status=400)
    if request.method == "DELETE":
        require_perm(request, "settings.sso", "write")
        deleted, _ = SSOProvider.all_objects.filter(organization_id=oid, provider=provider).delete()
        if not deleted:
            return error("SSO provider not found", http_status=404)
        return success({"message": "SSO provider deleted"})
    require_perm(request, "settings.sso", "write")
    data = request_body(request)
    if provider == "custom" and not (data.get("auth_url") and data.get("token_url") and data.get("user_info_url")):
        return error("Custom provider requires auth_url, token_url, and user_info_url", http_status=400)
    row = SSOProvider.all_objects.filter(organization_id=oid, provider=provider).first()
    if row is None:
        row = SSOProvider(organization_id=oid, provider=provider)
    row.client_id = data.get("client_id") or ""
    secret = data.get("client_secret") or ""
    if secret:
        row.client_secret = encrypt(secret, settings.ENCRYPTION_KEY)
    row.is_enabled = bool(data.get("is_enabled"))
    row.allow_auto_create = bool(data.get("allow_auto_create"))
    row.default_role_name = data.get("default_role") or "agent"
    row.allowed_domains = data.get("allowed_domains") or ""
    row.auth_url = data.get("auth_url") or ""
    row.token_url = data.get("token_url") or ""
    row.user_info_url = data.get("user_info_url") or ""
    row.save()
    return success(_provider_payload(row))
