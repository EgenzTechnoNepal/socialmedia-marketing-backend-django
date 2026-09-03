from rest_framework.decorators import api_view, permission_classes

from apps.billing.entitlements import FEATURE_CALLING, assert_feature
from apps.common.envelope import error
from apps.common.permissions import CookieAuthenticated, request_organization_id


def before_outgoing_call(organization_id):
    assert_feature(organization_id, FEATURE_CALLING)


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def initiate_outgoing_call(request):
    org_id = request_organization_id(request)
    before_outgoing_call(org_id)
    return error(
        "Calling is not ported to Django yet; feature check passed. Use the Go sidecar until Phase 4.",
        http_status=501,
        error_type="not_ported",
    )
