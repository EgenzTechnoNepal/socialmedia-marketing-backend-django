from rest_framework.decorators import api_view, permission_classes

from apps.billing.entitlements import FEATURE_AI, METER_AI_COMPLETION, assert_feature, record_usage
from apps.common.envelope import error
from apps.common.permissions import CookieAuthenticated, request_organization_id


def before_ai_completion(organization_id, completion_id: str):
    assert_feature(organization_id, FEATURE_AI)
    record_usage(organization_id, METER_AI_COMPLETION, event_id=f"ai:{completion_id}")


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def generate_ai(request):
    org_id = request_organization_id(request)
    event_id = request.data.get("session_id") or request.data.get("idempotency_key") or f"ai:{org_id}"
    before_ai_completion(org_id, str(event_id))
    return error(
        "AI chatbot is not ported to Django yet; entitlement check passed. Use the Go API until Phase 3.",
        http_status=501,
        error_type="not_ported",
    )
