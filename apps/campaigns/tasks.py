import logging
from datetime import datetime

from django.db.models import F
from django.utils import timezone as dj_tz

from apps.billing.entitlements import FEATURE_CAMPAIGNS, METER_CAMPAIGN_RECIPIENT, METER_MESSAGE_SENT, assert_feature, record_usage
from apps.campaigns.models import BulkMessageCampaign, BulkMessageRecipient
from apps.contacts.views import get_or_create_contact
from apps.messaging.models import Message
from apps.realtime.hub import broadcast_org
from apps.whatsapp.models import WhatsAppAccount
from config.celery import app
from services import whatsapp_client
from services.whatsapp_client import WhatsAppError

logger = logging.getLogger(__name__)


def _components(template, recipient, header_media_id):
    components = []
    header_type = (template.header_type or "").upper() if template else ""
    if header_type in {"IMAGE", "VIDEO", "DOCUMENT"} and header_media_id:
        kind = header_type.lower()
        components.append({"type": "header", "parameters": [{"type": kind, kind: {"id": header_media_id}}]})
    elif header_type == "TEXT":
        header_params = recipient.header_params or {}
        if header_params:
            components.append(
                {"type": "header", "parameters": [{"type": "text", "text": str(v)} for v in header_params.values()]}
            )
    params = recipient.template_params or {}
    if isinstance(params, dict) and params:
        components.append({"type": "body", "parameters": [{"type": "text", "text": str(v)} for v in params.values()]})
    elif isinstance(params, list) and params:
        components.append({"type": "body", "parameters": [{"type": "text", "text": str(v)} for v in params]})
    return components


def _publish_stats(campaign: BulkMessageCampaign):
    broadcast_org(
        campaign.organization_id,
        "campaign_stats_update",
        {
            "campaign_id": str(campaign.id),
            "status": campaign.status,
            "sent_count": campaign.sent_count,
            "delivered_count": campaign.delivered_count,
            "read_count": campaign.read_count,
            "failed_count": campaign.failed_count,
        },
    )


@app.task
def send_campaign_recipient(campaign_id: str, recipient_id: str):
    campaign = BulkMessageCampaign.objects.select_related("template").filter(id=campaign_id).first()
    recipient = BulkMessageRecipient.objects.filter(id=recipient_id).first()
    if not campaign or not recipient:
        return
    if campaign.status in {"paused", "cancelled"}:
        return
    try:
        assert_feature(campaign.organization_id, FEATURE_CAMPAIGNS)
    except Exception as exc:
        recipient.status = "failed"
        recipient.error_message = str(exc)
        recipient.save(update_fields=["status", "error_message", "updated_at"])
        BulkMessageCampaign.objects.filter(id=campaign.id).update(failed_count=F("failed_count") + 1)
        return
    account = WhatsAppAccount.objects.filter(
        organization_id=campaign.organization_id, name=campaign.whatsapp_account
    ).first()
    if account is None:
        recipient.status = "failed"
        recipient.error_message = "WhatsApp account not found"
        recipient.save(update_fields=["status", "error_message", "updated_at"])
        BulkMessageCampaign.objects.filter(id=campaign.id).update(failed_count=F("failed_count") + 1)
        return
    account.decrypt_secrets()
    contact, _ = get_or_create_contact(
        campaign.organization_id, recipient.phone_number, recipient.recipient_name or "", campaign.whatsapp_account
    )
    template = campaign.template
    if contact.marketing_opt_out and template and (template.category or "").upper() == "MARKETING":
        recipient.status = "failed"
        recipient.error_message = "Contact opted out of marketing messages"
        recipient.save(update_fields=["status", "error_message", "updated_at"])
        BulkMessageCampaign.objects.filter(id=campaign.id).update(failed_count=F("failed_count") + 1)
        _maybe_complete(campaign)
        return
    components = _components(template, recipient, campaign.header_media_id)
    wamid = ""
    err = ""
    try:
        wamid = whatsapp_client.send_template(
            account, recipient.phone_number, template.name, template.language, components
        )
        record_usage(campaign.organization_id, METER_CAMPAIGN_RECIPIENT, event_id=f"campaign-rcpt:{recipient.id}")
        record_usage(campaign.organization_id, METER_MESSAGE_SENT, event_id=f"campaign-msg:{recipient.id}")
    except WhatsAppError as exc:
        err = str(exc)
    msg = Message.objects.create(
        organization_id=campaign.organization_id,
        whatsapp_account=campaign.whatsapp_account,
        contact=contact,
        whatsapp_message_id=wamid,
        direction="outgoing",
        message_type="template",
        content=(template.body_content if template else "") or "",
        template_name=template.name if template else "",
        template_params=recipient.template_params or {},
        media_url=campaign.header_media_local_path or "",
        media_mime_type=campaign.header_media_mime_type or "",
        status="failed" if err else "sent",
        error_message=err,
        metadata={"campaign_id": str(campaign.id), "recipient_name": recipient.recipient_name or ""},
    )
    if err:
        recipient.status = "failed"
        recipient.error_message = err
        BulkMessageCampaign.objects.filter(id=campaign.id).update(failed_count=F("failed_count") + 1)
    else:
        recipient.status = "sent"
        recipient.whatsapp_message_id = wamid
        recipient.sent_at = dj_tz.now()
        recipient.message = msg
        BulkMessageCampaign.objects.filter(id=campaign.id).update(sent_count=F("sent_count") + 1)
    recipient.save()
    _maybe_complete(campaign)


def _maybe_complete(campaign: BulkMessageCampaign):
    pending = BulkMessageRecipient.objects.filter(campaign=campaign, status="pending").count()
    campaign = BulkMessageCampaign.objects.get(id=campaign.id)
    if pending == 0 and campaign.status == "processing":
        campaign.status = "completed"
        campaign.completed_at = dj_tz.now()
        campaign.save(update_fields=["status", "completed_at", "updated_at"])
    _publish_stats(campaign)
