from rest_framework.decorators import api_view, permission_classes

from apps.billing.entitlements import (
    FEATURE_CAMPAIGNS,
    METER_CAMPAIGN_RECIPIENT,
    METER_MESSAGE_SENT,
    assert_feature,
    record_usage,
)
from apps.common.envelope import error
from apps.common.permissions import CookieAuthenticated, request_organization_id


def before_create_campaign(organization_id):
    assert_feature(organization_id, FEATURE_CAMPAIGNS)


def before_send_recipient(organization_id, recipient_id: str):
    assert_feature(organization_id, FEATURE_CAMPAIGNS)
    record_usage(organization_id, METER_CAMPAIGN_RECIPIENT, event_id=f"campaign-rcpt:{recipient_id}")
    record_usage(organization_id, METER_MESSAGE_SENT, event_id=f"campaign-msg:{recipient_id}")


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def create_campaign(request):
    org_id = request_organization_id(request)
    before_create_campaign(org_id)
    return error(
        "Campaigns are not ported to Django yet; feature check passed. Use the Go API until Phase 3.",
        http_status=501,
        error_type="not_ported",
    )


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def start_campaign(request, campaign_id):
    org_id = request_organization_id(request)
    assert_feature(org_id, FEATURE_CAMPAIGNS)
    return error(
        "Campaign start is not ported to Django yet; feature check passed.",
        http_status=501,
        error_type="not_ported",
    )
