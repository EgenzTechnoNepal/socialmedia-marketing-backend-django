import logging

import bcrypt
import jwt
from django.conf import settings
from django.db import transaction
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny

from apps.accounts.models import CustomRole, Organization, User, UserOrganization
from apps.accounts.payloads import apply_org_role, user_to_response
from apps.common.cookies import (
    access_expires_in_seconds,
    clear_auth_cookies,
    set_auth_cookies,
)
from apps.common.envelope import error, success
from apps.common.permissions import CookieAuthenticated
from apps.common.tokens import (
    DUMMY_BCRYPT,
    consume_refresh_jti,
    decode_token,
    generate_access_token,
    generate_refresh_token,
    generate_ws_token,
    revoke_refresh_jti,
)

logger = logging.getLogger(__name__)


def _issue_auth(user: User, organization_id=None):
    org_id = organization_id if organization_id is not None else user.organization_id
    apply_org_role(user, org_id)
    access = generate_access_token(user, org_id)
    refresh = generate_refresh_token(user, org_id)
    response = success(
        {
            "expires_in": access_expires_in_seconds(),
            "user": user_to_response(user),
        }
    )
    return set_auth_cookies(response, access, refresh)


def _dummy_bcrypt(password: str) -> None:
    try:
        bcrypt.checkpw(password.encode("utf-8"), DUMMY_BCRYPT)
    except ValueError:
        pass


def _refresh_from_request(request) -> str:
    token = request.COOKIES.get(settings.COOKIE_REFRESH_NAME) or ""
    if token:
        return token
    data = request.data if isinstance(request.data, dict) else {}
    return data.get("refresh_token") or ""


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def login(request):
    data = request.data if isinstance(request.data, dict) else {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if not email or not password:
        return error("Invalid credentials", http_status=401)

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        _dummy_bcrypt(password)
        return error("Invalid credentials", http_status=401)

    if not user.is_active:
        return error("Account is disabled", http_status=401)

    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("utf-8"))
    except ValueError:
        ok = False
    if not ok:
        return error("Invalid credentials", http_status=401)

    return _issue_auth(user)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def register(request):
    data = request.data if isinstance(request.data, dict) else {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    full_name = (data.get("full_name") or "").strip()
    organization_id = data.get("organization_id")
    if not email or not password or not full_name or not organization_id:
        return error("email, password, full_name, and organization_id are required", http_status=400)

    try:
        org = Organization.objects.get(id=organization_id)
    except Organization.DoesNotExist:
        return error("Organization not found", http_status=404)

    default_role = (
        CustomRole.objects.filter(organization=org, is_default=True).first()
        or CustomRole.objects.filter(organization=org, name="agent", is_system=True).first()
    )
    if not default_role:
        return error("Failed to find default role", http_status=500)

    existing = User.objects.filter(email=email).first()
    if existing:
        try:
            ok = bcrypt.checkpw(password.encode("utf-8"), existing.password_hash.encode("utf-8"))
        except ValueError:
            ok = False
        if not ok:
            return error(
                "An account with this email already exists. Please sign in and ask your organization admin to add you.",
                http_status=409,
            )
        if not existing.is_active:
            return error("Account is disabled", http_status=401)
        if UserOrganization.objects.filter(user=existing, organization=org).exists():
            return error("You are already a member of this organization", http_status=409)
        UserOrganization.objects.create(
            user=existing,
            organization=org,
            role_id=default_role.id,
            is_default=False,
        )
        existing.role_id = default_role.id
        return _issue_auth(existing, org.id)

    _dummy_bcrypt(password)
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")
    try:
        with transaction.atomic():
            user = User.objects.create(
                organization=org,
                email=email,
                password_hash=password_hash,
                full_name=full_name,
                role_id=default_role.id,
                is_active=True,
            )
            UserOrganization.objects.create(
                user=user,
                organization=org,
                role_id=default_role.id,
                is_default=True,
            )
    except Exception:
        logger.exception("Failed to create account")
        return error("Failed to create account", http_status=500)

    return _issue_auth(user, org.id)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def refresh(request):
    refresh_token = _refresh_from_request(request)
    if not refresh_token:
        return error("Missing refresh token", http_status=401)

    try:
        claims = decode_token(refresh_token, verify_exp=True)
    except jwt.ExpiredSignatureError:
        return error("Invalid refresh token", http_status=401)
    except jwt.InvalidTokenError:
        return error("Invalid refresh token", http_status=401)

    jti = claims.get("jti") or ""
    if jti and not consume_refresh_jti(jti):
        return error("Refresh token has been revoked", http_status=401)

    user_id = claims.get("user_id")
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return error("User not found", http_status=401)

    if not user.is_active:
        return error("Account is disabled", http_status=401)

    org_id = claims.get("organization_id") or user.organization_id
    if claims.get("role_id"):
        user.role_id = claims["role_id"]
    return _issue_auth(user, org_id)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def logout(request):
    refresh_token = _refresh_from_request(request)
    if refresh_token:
        try:
            claims = decode_token(refresh_token, verify_exp=False)
            revoke_refresh_jti(claims.get("jti") or "")
        except jwt.InvalidTokenError:
            pass
    response = success({"status": "logged_out"})
    return clear_auth_cookies(response)


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def switch_org(request):
    data = request.data if isinstance(request.data, dict) else {}
    organization_id = data.get("organization_id")
    if not organization_id:
        return error("organization_id is required", http_status=400)

    try:
        org = Organization.objects.get(id=organization_id)
    except Organization.DoesNotExist:
        return error("Organization not found", http_status=404)

    user = request.user
    if not user.is_super_admin:
        membership = UserOrganization.objects.filter(user=user, organization=org).first()
        if not membership:
            return error("You are not a member of this organization", http_status=403)
        if membership.role_id:
            user.role_id = membership.role_id

    return _issue_auth(user, org.id)


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def me(request):
    user = request.user
    org_id = getattr(user, "token_organization_id", None) or user.organization_id
    apply_org_role(user, org_id)
    return success(user_to_response(user))


@api_view(["PUT"])
@permission_classes([CookieAuthenticated])
def me_settings(request):
    data = request.data if isinstance(request.data, dict) else {}
    settings = request.user.settings or {}
    for key in ("email_notifications", "new_message_alerts", "campaign_updates"):
        if key in data:
            settings[key] = bool(data[key])
    request.user.settings = settings
    request.user.save(update_fields=["settings", "updated_at"])
    return success(settings)


@api_view(["PUT"])
@permission_classes([CookieAuthenticated])
def me_password(request):
    data = request.data if isinstance(request.data, dict) else {}
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""
    if not current_password or not new_password:
        return error("current_password and new_password are required", http_status=400)
    try:
        ok = bcrypt.checkpw(current_password.encode("utf-8"), request.user.password_hash.encode("utf-8"))
    except ValueError:
        ok = False
    if not ok:
        return error("Current password is incorrect", http_status=400)
    request.user.password_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")
    request.user.save(update_fields=["password_hash", "updated_at"])
    return success({"message": "Password updated"})


@api_view(["PUT"])
@permission_classes([CookieAuthenticated])
def me_availability(request):
    data = request.data if isinstance(request.data, dict) else {}
    request.user.is_available = bool(data.get("is_available", True))
    request.user.save(update_fields=["is_available", "updated_at"])
    return success({"is_available": request.user.is_available})


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def me_organizations(request):
    from apps.accounts.models import CustomRole, Organization, UserOrganization

    rows = UserOrganization.objects.filter(user=request.user)
    payload = []
    for row in rows:
        org = Organization.objects.filter(id=row.organization_id).first()
        if not org:
            continue
        role_name = ""
        if row.role_id:
            role = CustomRole.objects.filter(id=row.role_id).first()
            role_name = role.name if role else ""
        payload.append(
            {
                "organization_id": str(row.organization_id),
                "name": org.name,
                "slug": org.slug,
                "role_id": str(row.role_id) if row.role_id else None,
                "role_name": role_name,
                "is_default": row.is_default,
            }
        )
    return success({"organizations": payload})


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def ws_token(request):
    from apps.common.permissions import request_organization_id

    token = generate_ws_token(request.user.id, request_organization_id(request))
    return success({"token": token})
