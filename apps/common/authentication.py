import jwt
from django.conf import settings
from rest_framework import authentication, exceptions

from apps.accounts.models import APIKey, User


class CookieJWTAuthentication(authentication.BaseAuthentication):
    """Read the httpOnly access cookie (whm_access)."""

    def authenticate(self, request):
        token = request.COOKIES.get(settings.COOKIE_ACCESS_NAME)
        if not token:
            header = request.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                token = header[7:]
        if not token:
            return None

        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET,
                algorithms=["HS256"],
                issuer=settings.JWT_ISSUER,
                options={"require": ["exp"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise exceptions.AuthenticationFailed("Token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise exceptions.AuthenticationFailed("Invalid token") from exc

        user_id = payload.get("user_id")
        if not user_id:
            raise exceptions.AuthenticationFailed("Invalid token")

        try:
            user = User.objects.get(id=user_id, is_active=True)
        except User.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed("User not found") from exc

        org_id = payload.get("organization_id") or str(user.organization_id)
        user.token_organization_id = org_id
        user.is_super_admin_claim = bool(payload.get("is_super_admin") or user.is_super_admin)
        if payload.get("role_id"):
            user.role_id = payload["role_id"]
        return (user, payload)

    def authenticate_header(self, request):
        return "Bearer"


class APIKeyAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        raw = request.headers.get("X-API-Key")
        if not raw:
            return None
        api_key = APIKey.match(raw)
        if not api_key:
            raise exceptions.AuthenticationFailed("Invalid API key")
        user = api_key.user
        user.token_organization_id = str(api_key.organization_id)
        user.is_super_admin_claim = bool(user.is_super_admin)
        return (user, api_key)

    def authenticate_header(self, request):
        return "ApiKey"
