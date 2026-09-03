from datetime import datetime

from django.db.models import F, Q
from django.http import FileResponse
from django.utils import timezone as dj_tz
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser

from apps.billing.entitlements import FEATURE_CAMPAIGNS, assert_feature
from apps.campaigns.models import BulkMessageCampaign, BulkMessageRecipient
from apps.campaigns.tasks import send_campaign_recipient
from apps.common.envelope import error, success
from apps.common.http import iso, list_payload, org_id, parse_pagination, request_body, require_perm, soft_delete
from apps.common.permissions import CookieAuthenticated
from apps.messaging.models import Template
from apps.whatsapp.models import WhatsAppAccount
from services import storage, whatsapp_client
from services.whatsapp_client import WhatsAppError


def campaign_payload(row: BulkMessageCampaign) -> dict:
    template = getattr(row, "template", None)
    creator = getattr(row, "created_by", None)
    updated = getattr(row, "updated_by", None)
    return {
        "id": str(row.id),
        "name": row.name,
        "whatsapp_account": row.whatsapp_account,
        "template_id": str(row.template_id),
        "template_name": template.name if template else "",
        "header_media_id": row.header_media_id or "",
        "header_media_filename": row.header_media_filename or "",
        "header_media_mime_type": row.header_media_mime_type or "",
        "status": row.status,
        "total_recipients": row.total_recipients or 0,
        "sent_count": row.sent_count or 0,
        "delivered_count": row.delivered_count or 0,
        "read_count": row.read_count or 0,
        "failed_count": row.failed_count or 0,
        "scheduled_at": iso(row.scheduled_at),
        "started_at": iso(row.started_at),
        "completed_at": iso(row.completed_at),
        "created_by_name": creator.full_name if creator else "",
        "updated_by_name": updated.full_name if updated else "",
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


def _get_campaign(oid, campaign_id) -> BulkMessageCampaign:
    campaign = (
        BulkMessageCampaign.objects.filter(id=campaign_id, organization_id=oid)
        .select_related("template", "created_by", "updated_by")
        .first()
    )
    if not campaign:
        from apps.common.exceptions import APIError

        raise APIError("Campaign not found", status_code=404)
    return campaign


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def campaigns_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        require_perm(request, "campaigns", "read")
        page, limit, offset = parse_pagination(request)
        qs = BulkMessageCampaign.objects.filter(organization_id=oid).select_related("template")
        search = request.query_params.get("search") or ""
        if search:
            qs = qs.filter(name__icontains=search)
        status = request.query_params.get("status") or ""
        if status and status != "all":
            qs = qs.filter(status=status)
        account = request.query_params.get("whatsapp_account") or ""
        if account:
            qs = qs.filter(whatsapp_account=account)
        date_from = request.query_params.get("from") or ""
        date_to = request.query_params.get("to") or ""
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        total = qs.count()
        rows = list(qs.order_by("-created_at")[offset : offset + limit])
        return success(list_payload("campaigns", [campaign_payload(c) for c in rows], total, page, limit))
    require_perm(request, "campaigns", "write")
    assert_feature(oid, FEATURE_CAMPAIGNS)
    data = request_body(request)
    if not data.get("name") or not data.get("whatsapp_account") or not data.get("template_id"):
        return error("name, whatsapp_account and template_id are required", http_status=400)
    template = Template.objects.filter(id=data["template_id"], organization_id=oid).first()
    if not template:
        return error("Template not found", http_status=404)
    if not WhatsAppAccount.objects.filter(organization_id=oid, name=data["whatsapp_account"]).exists():
        return error("WhatsApp account not found", http_status=400)
    scheduled = data.get("scheduled_at")
    campaign = BulkMessageCampaign.objects.create(
        organization_id=oid,
        name=data["name"],
        whatsapp_account=data["whatsapp_account"],
        template=template,
        header_media_id=data.get("header_media_id") or "",
        status="draft",
        scheduled_at=scheduled,
        created_by=request.user,
        updated_by=request.user,
    )
    campaign = BulkMessageCampaign.objects.select_related("template", "created_by").get(id=campaign.id)
    return success(campaign_payload(campaign), http_status=201)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def campaign_detail(request, campaign_id):
    oid = org_id(request)
    campaign = _get_campaign(oid, campaign_id)
    if request.method == "GET":
        require_perm(request, "campaigns", "read")
        return success(campaign_payload(campaign))
    if request.method == "DELETE":
        require_perm(request, "campaigns", "delete")
        if campaign.status in {"processing", "queued"}:
            return error("Cannot delete running campaign", http_status=400)
        soft_delete(campaign)
        return success({"message": "Campaign deleted successfully"})
    require_perm(request, "campaigns", "write")
    if campaign.status != "draft":
        return error("Can only update draft campaigns", http_status=400)
    data = request_body(request)
    if data.get("name"):
        campaign.name = data["name"]
    if data.get("whatsapp_account"):
        campaign.whatsapp_account = data["whatsapp_account"]
    if data.get("template_id"):
        template = Template.objects.filter(id=data["template_id"], organization_id=oid).first()
        if not template:
            return error("Template not found", http_status=404)
        campaign.template = template
    if "scheduled_at" in data:
        campaign.scheduled_at = data.get("scheduled_at")
    campaign.updated_by = request.user
    campaign.save()
    return success(campaign_payload(_get_campaign(oid, campaign_id)))


def _enqueue_pending(campaign: BulkMessageCampaign):
    recipients = BulkMessageRecipient.objects.filter(campaign=campaign, status="pending")
    count = 0
    for recipient in recipients:
        send_campaign_recipient.delay(str(campaign.id), str(recipient.id))
        count += 1
    return count


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def start_campaign(request, campaign_id):
    oid = org_id(request)
    require_perm(request, "campaigns", "execute")
    assert_feature(oid, FEATURE_CAMPAIGNS)
    campaign = _get_campaign(oid, campaign_id)
    if campaign.status not in {"draft", "scheduled", "paused"}:
        return error("Campaign cannot be started in current state", http_status=400)
    pending = BulkMessageRecipient.objects.filter(campaign=campaign, status="pending").count()
    if pending == 0:
        return error("Campaign has no pending recipients", http_status=400)
    campaign.status = "processing"
    campaign.started_at = campaign.started_at or dj_tz.now()
    campaign.save(update_fields=["status", "started_at", "updated_at"])
    _enqueue_pending(campaign)
    return success({"message": "Campaign started", "status": "processing"})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def pause_campaign(request, campaign_id):
    oid = org_id(request)
    require_perm(request, "campaigns", "execute")
    campaign = _get_campaign(oid, campaign_id)
    if campaign.status not in {"processing", "queued"}:
        return error("Campaign is not running", http_status=400)
    campaign.status = "paused"
    campaign.save(update_fields=["status", "updated_at"])
    return success({"message": "Campaign paused", "status": "paused"})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def cancel_campaign(request, campaign_id):
    oid = org_id(request)
    require_perm(request, "campaigns", "execute")
    campaign = _get_campaign(oid, campaign_id)
    if campaign.status in {"completed", "cancelled"}:
        return error("Campaign already finished", http_status=400)
    campaign.status = "cancelled"
    campaign.save(update_fields=["status", "updated_at"])
    return success({"message": "Campaign cancelled", "status": "cancelled"})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def retry_failed(request, campaign_id):
    oid = org_id(request)
    require_perm(request, "campaigns", "execute")
    assert_feature(oid, FEATURE_CAMPAIGNS)
    campaign = _get_campaign(oid, campaign_id)
    if campaign.status not in {"completed", "paused", "failed"}:
        return error("Can only retry failed messages on completed, paused, or failed campaigns", http_status=400)
    updated = BulkMessageRecipient.objects.filter(campaign=campaign, status="failed").update(
        status="pending", error_message="", updated_at=dj_tz.now()
    )
    campaign.status = "processing"
    campaign.save(update_fields=["status", "updated_at"])
    _enqueue_pending(campaign)
    return success({"message": "Retry started", "status": "processing", "retried": updated})


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def campaign_progress(request, campaign_id):
    oid = org_id(request)
    require_perm(request, "campaigns", "read")
    return success(campaign_payload(_get_campaign(oid, campaign_id)))


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def campaign_recipients(request, campaign_id):
    oid = org_id(request)
    campaign = _get_campaign(oid, campaign_id)
    if request.method == "GET":
        require_perm(request, "campaigns", "read")
        rows = list(BulkMessageRecipient.objects.filter(campaign=campaign).order_by("created_at"))
        items = [
            {
                "id": str(r.id),
                "phone_number": r.phone_number,
                "recipient_name": r.recipient_name or "",
                "template_params": r.template_params or {},
                "header_params": r.header_params or {},
                "status": r.status,
                "whatsapp_message_id": r.whatsapp_message_id or "",
                "error_message": r.error_message or "",
                "sent_at": iso(r.sent_at),
                "created_at": iso(r.created_at),
            }
            for r in rows
        ]
        return success({"recipients": items, "total": len(items)})
    return error("Use /recipients/import", http_status=405)


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def import_recipients(request, campaign_id):
    oid = org_id(request)
    require_perm(request, "campaigns", "write")
    campaign = _get_campaign(oid, campaign_id)
    if campaign.status != "draft":
        return error("Can only add recipients to draft campaigns", http_status=400)
    data = request_body(request)
    items = data.get("recipients") or []
    created = []
    now = dj_tz.now()
    for rec in items:
        phone = rec.get("phone_number") or ""
        if not phone:
            continue
        created.append(
            BulkMessageRecipient(
                campaign=campaign,
                phone_number=phone,
                recipient_name=rec.get("recipient_name") or "",
                template_params=rec.get("template_params") or {},
                header_params=rec.get("header_params") or {},
                status="pending",
                created_at=now,
                updated_at=now,
            )
        )
    if created:
        BulkMessageRecipient.objects.bulk_create(created)
    total = BulkMessageRecipient.objects.filter(campaign=campaign).count()
    campaign.total_recipients = total
    campaign.save(update_fields=["total_recipients", "updated_at"])
    return success({"message": "Recipients added successfully", "added_count": len(created), "total_recipients": total})


@api_view(["DELETE"])
@permission_classes([CookieAuthenticated])
def delete_recipient(request, campaign_id, recipient_id):
    oid = org_id(request)
    require_perm(request, "campaigns", "write")
    campaign = _get_campaign(oid, campaign_id)
    if campaign.status != "draft":
        return error("Can only delete recipients from draft campaigns", http_status=400)
    recipient = BulkMessageRecipient.objects.filter(id=recipient_id, campaign=campaign).first()
    if not recipient:
        return error("Recipient not found", http_status=404)
    soft_delete(recipient)
    BulkMessageCampaign.objects.filter(id=campaign.id).update(total_recipients=F("total_recipients") - 1)
    return success({"message": "Recipient deleted successfully"})


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def campaign_media(request, campaign_id):
    oid = org_id(request)
    campaign = _get_campaign(oid, campaign_id)
    if request.method == "GET":
        require_perm(request, "campaigns", "read")
        path = campaign.header_media_local_path
        if not path:
            return error("No media uploaded", http_status=404)
        try:
            from django.conf import settings as dj_settings
            from services.storage import absolute_path

            handle = open(absolute_path(path), "rb")
        except Exception:
            return error("Media file not found", http_status=404)
        return FileResponse(handle, content_type=campaign.header_media_mime_type or "application/octet-stream")
    require_perm(request, "campaigns", "write")
    if campaign.status != "draft":
        return error("Can only upload media for draft campaigns", http_status=400)
    template = campaign.template
    if not template or not template.header_type or template.header_type.upper() == "TEXT":
        return error("Template does not have a media header", http_status=400)
    upload = request.FILES.get("file")
    if not upload:
        return error("file is required", http_status=400)
    account = WhatsAppAccount.objects.filter(organization_id=oid, name=campaign.whatsapp_account).first()
    if not account:
        return error("WhatsApp account not found", http_status=400)
    account.decrypt_secrets()
    data = upload.read()
    filename = upload.name or "header.bin"
    mime = upload.content_type or "application/octet-stream"
    relative = f"campaigns/{campaign.id}_{filename}"
    storage.save_bytes(relative, data)
    try:
        media_id = whatsapp_client.upload_media(account, data, mime, filename)
    except WhatsAppError as exc:
        return error(str(exc), http_status=502)
    campaign.header_media_id = media_id
    campaign.header_media_filename = filename
    campaign.header_media_mime_type = mime
    campaign.header_media_local_path = relative
    campaign.save(
        update_fields=[
            "header_media_id",
            "header_media_filename",
            "header_media_mime_type",
            "header_media_local_path",
            "updated_at",
        ]
    )
    return success(
        {
            "header_media_id": media_id,
            "header_media_filename": filename,
            "header_media_mime_type": mime,
        }
    )
