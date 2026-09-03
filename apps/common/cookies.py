import secrets

from django.conf import settings
from rest_framework.response import Response


def _domain():
    return settings.COOKIE_DOMAIN or None


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> Response:
    secure = settings.COOKIE_SECURE
    domain = _domain()
    access_max = settings.JWT_ACCESS_EXPIRY_MINS * 60
    refresh_max = settings.JWT_REFRESH_EXPIRY_DAYS * 86400

    response.set_cookie(
        settings.COOKIE_ACCESS_NAME,
        access_token,
        max_age=access_max,
        httponly=True,
        secure=secure,
        samesite="Lax",
        path="/api",
        domain=domain,
    )
    response.set_cookie(
        settings.COOKIE_REFRESH_NAME,
        refresh_token,
        max_age=refresh_max,
        httponly=True,
        secure=secure,
        samesite="Lax",
        path="/api/auth/refresh",
        domain=domain,
    )
    response.set_cookie(
        settings.COOKIE_CSRF_NAME,
        secrets.token_urlsafe(32),
        max_age=refresh_max,
        httponly=False,
        secure=secure,
        samesite="Lax",
        path="/",
        domain=domain,
    )
    return response


def clear_auth_cookies(response: Response) -> Response:
    secure = settings.COOKIE_SECURE
    domain = _domain()
    specs = (
        (settings.COOKIE_ACCESS_NAME, "/api", True),
        (settings.COOKIE_REFRESH_NAME, "/api/auth/refresh", True),
        (settings.COOKIE_CSRF_NAME, "/", False),
    )
    for name, path, httponly in specs:
        response.set_cookie(
            name,
            "",
            max_age=0,
            expires=0,
            httponly=httponly,
            secure=secure,
            samesite="Lax",
            path=path,
            domain=domain,
        )
    return response


def access_expires_in_seconds() -> int:
    return settings.JWT_ACCESS_EXPIRY_MINS * 60
