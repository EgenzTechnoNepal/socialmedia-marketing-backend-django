from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from apps.billing.entitlements import FEATURE_CALLING, assert_feature
from apps.common.envelope import error, success
from apps.common.permissions import CookieAuthenticated, request_organization_id

GO_SIDECAR_MSG = (
    "Calling/IVR is served by the Go sidecar on :8080. This Django process does not handle live calls."
)


def before_outgoing_call(organization_id):
    assert_feature(organization_id, FEATURE_CALLING)


@api_view(["GET", "POST", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def not_ported(request, **kwargs):
    return error(GO_SIDECAR_MSG, http_status=501, error_type="not_ported")


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def initiate_outgoing_call(request):
    org_id = request_organization_id(request)
    before_outgoing_call(org_id)
    return error(GO_SIDECAR_MSG, http_status=501, error_type="not_ported")


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def call_permission(request, contact_id):
    return success({"status": "none"})


@api_view(["GET"])
@permission_classes([AllowAny])
def ice_servers(request):
    return success({"ice_servers": []})
