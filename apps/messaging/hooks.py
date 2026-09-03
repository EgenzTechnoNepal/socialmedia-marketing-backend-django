from rest_framework.decorators import api_view, permission_classes

from apps.billing.entitlements import METER_MESSAGE_SENT, assert_outbound_allowed, record_usage
from apps.common.envelope import error
from apps.common.permissions import CookieAuthenticated, request_organization_id


def before_send_message(organization_id, message_id: str):
    assert_outbound_allowed(organization_id)
    record_usage(organization_id, METER_MESSAGE_SENT, event_id=f"msg:{message_id}")


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def send_message(request, contact_id=None):
    org_id = request_organization_id(request)
    event_id = request.data.get("client_id") or request.data.get("idempotency_key") or f"draft:{org_id}"
    before_send_message(org_id, str(event_id))
    return error(
        "Send message is not ported to Django yet; usage/entitlement check passed. Use the Go API until Phase 2.",
        http_status=501,
        error_type="not_ported",
    )
