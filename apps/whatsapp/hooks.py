from rest_framework.decorators import api_view, permission_classes

from apps.billing.entitlements import (
    FEATURE_EXTRA_WA,
    assert_can_add_whatsapp_account,
    assert_feature,
)
from apps.common.envelope import error
from apps.common.permissions import CookieAuthenticated, request_organization_id


def before_create_account(organization_id, existing_count: int):
    sub = assert_can_add_whatsapp_account(organization_id)
    if existing_count >= 1:
        assert_feature(organization_id, FEATURE_EXTRA_WA)
    return sub


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def create_account(request):
    org_id = request_organization_id(request)
    existing = int(request.data.get("existing_count") or 0)
    before_create_account(org_id, existing)
    return error(
        "WhatsApp accounts are not ported to Django yet; feature check passed.",
        http_status=501,
        error_type="not_ported",
    )
