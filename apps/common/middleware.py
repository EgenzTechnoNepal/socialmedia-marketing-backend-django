from django.conf import settings
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin

CSRF_EXEMPT_PREFIXES = (
    "/health",
    "/ready",
    "/api/webhooks/",
    "/api/webhook",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/refresh",
)


class GoStyleCSRFMiddleware(MiddlewareMixin):
    """Match Go: mutating requests must send X-CSRF-Token equal to the whm_csrf cookie."""

    SAFE = {"GET", "HEAD", "OPTIONS", "TRACE"}

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method in self.SAFE:
            return None
        path = request.path
        if any(path == prefix or path.startswith(prefix) for prefix in CSRF_EXEMPT_PREFIXES):
            return None
        if request.headers.get("Authorization") or request.headers.get("X-API-Key"):
            return None
        cookie = request.COOKIES.get(settings.COOKIE_CSRF_NAME)
        header = request.headers.get("X-CSRF-Token")
        if not cookie or not header or cookie != header:
            return JsonResponse(
                {"status": "error", "message": "CSRF token missing or invalid", "error_type": "csrf"},
                status=403,
            )
        return None


class OrganizationHeaderMiddleware(MiddlewareMixin):
    """X-Organization-ID overrides JWT org for super admins (same as Go)."""

    def process_request(self, request):
        request.organization_id_override = request.headers.get("X-Organization-ID") or None
