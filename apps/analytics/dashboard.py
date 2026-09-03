from apps.campaigns.models import BulkMessageCampaign
from apps.chatbot.models import ChatbotSession
from apps.common.envelope import success
from apps.common.http import iso, org_id, pct_change, period_bounds, previous_period, require_perm
from apps.contacts.models import Contact
from apps.messaging.models import Message
from rest_framework.decorators import api_view, permission_classes

from apps.common.permissions import CookieAuthenticated


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def dashboard_stats(request):
    oid = org_id(request)
    require_perm(request, "analytics", "read")
    start, end, err = period_bounds(request)
    if err:
        from apps.common.envelope import error

        return error(err, http_status=400)
    prev_start, prev_end = previous_period(start, end)

    def _count(model, extra=None, start_at=start, end_at=end, field="created_at"):
        qs = model.objects.filter(organization_id=oid, **{f"{field}__gte": start_at, f"{field}__lte": end_at})
        if extra:
            qs = qs.filter(**extra)
        return qs.count()

    current_messages = _count(Message)
    prev_messages = _count(Message, start_at=prev_start, end_at=prev_end)
    current_contacts = _count(Contact)
    prev_contacts = _count(Contact, start_at=prev_start, end_at=prev_end)
    current_sessions = _count(ChatbotSession)
    prev_sessions = _count(ChatbotSession, start_at=prev_start, end_at=prev_end)
    campaign_extra = {"status__in": ["completed", "processing"]}
    current_campaigns = _count(BulkMessageCampaign, extra=campaign_extra)
    prev_campaigns = _count(BulkMessageCampaign, extra=campaign_extra, start_at=prev_start, end_at=prev_end)

    stats = {
        "total_messages": current_messages,
        "messages_change": pct_change(prev_messages, current_messages),
        "total_contacts": current_contacts,
        "contacts_change": pct_change(prev_contacts, current_contacts),
        "chatbot_sessions": current_sessions,
        "chatbot_change": pct_change(prev_sessions, current_sessions),
        "campaigns_sent": current_campaigns,
        "campaigns_change": pct_change(prev_campaigns, current_campaigns),
    }
    recent = []
    for msg in Message.objects.filter(organization_id=oid).select_related("contact").order_by("-created_at")[:5]:
        contact = msg.contact
        name = "Unknown"
        if contact:
            name = contact.profile_name or contact.phone_number or "Unknown"
        content = msg.content or ""
        if not content and msg.message_type != "text":
            content = f"[{msg.message_type}]"
        recent.append(
            {
                "id": str(msg.id),
                "contact_name": name,
                "content": content,
                "direction": msg.direction,
                "created_at": iso(msg.created_at),
                "status": msg.status,
            }
        )
    return success({"stats": stats, "recent_messages": recent})


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def message_analytics(request):
    org_id(request)
    from apps.common.envelope import error

    return error("Not implemented yet", http_status=501)


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def chatbot_analytics(request):
    org_id(request)
    from apps.common.envelope import error

    return error("Not implemented yet", http_status=501)
