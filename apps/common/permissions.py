from rest_framework.exceptions import NotAuthenticated
from rest_framework.permissions import BasePermission


def request_organization_id(request):
    user = request.user
    override = getattr(request, "organization_id_override", None)
    token_org = getattr(user, "token_organization_id", None)
    if override and getattr(user, "is_super_admin_claim", False):
        return override
    return override or token_org or str(getattr(user, "organization_id", "") or "")


class CookieAuthenticated(BasePermission):
    """401 when no JWT cookie, matching Go."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not getattr(user, "is_authenticated", False):
            raise NotAuthenticated()
        return True


class HasOrg(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request_organization_id(request))


class HasBillingAccess(BasePermission):
    """settings.billing:read for GET, :write for mutating. Super admins always pass."""

    def has_permission(self, request, view):
        user = request.user
        if not user:
            return False
        if getattr(user, "is_super_admin_claim", False) or getattr(user, "is_super_admin", False):
            return True
        action = "read" if request.method in ("GET", "HEAD", "OPTIONS") else "write"
        return user.has_permission("settings.billing", action)
