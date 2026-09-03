import logging
import uuid

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone as dj_tz
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import User
from apps.chatbot.assignment import assign_to_team, mark_pickup, set_sla_deadlines
from apps.chatbot.models import AgentTransfer, ChatbotSession, ChatbotSettings, Team, TeamMember
from apps.common.envelope import error, success
from apps.common.exceptions import APIError
from apps.common.http import iso, org_id, request_body, require_perm, uid
from apps.common.permissions import CookieAuthenticated
from apps.contacts.models import Contact
from apps.realtime.hub import broadcast_org
from apps.webhooks.dispatch import (
    EVENT_TRANSFER_ASSIGNED,
    EVENT_TRANSFER_CREATED,
    EVENT_TRANSFER_RESUMED,
    dispatch_webhook,
)
from apps.whatsapp.models import WhatsAppAccount

logger = logging.getLogger(__name__)


def load_settings(org, account_name=""):
    from apps.chatbot.processor import load_settings as _load

    return _load(org, account_name)


def has_active_transfer(org, contact_id) -> bool:
    return AgentTransfer.objects.filter(organization_id=org, contact_id=contact_id, status="active").exists()


def transfer_payload(row: AgentTransfer, *, contact_name="", agent_name="", team_name="", transferred_by_name="", resumed_by_name=""):
    def opt(value):
        return str(value) if value else None

    return {
        "id": str(row.id),
        "contact_id": str(row.contact_id),
        "contact_name": contact_name or "",
        "phone_number": row.phone_number,
        "whatsapp_account": row.whatsapp_account,
        "status": row.status,
        "source": row.source,
        "agent_id": opt(row.agent_id),
        "agent_name": agent_name or None,
        "team_id": opt(row.team_id),
        "team_name": team_name or None,
        "transferred_by": opt(row.transferred_by_user_id),
        "transferred_by_name": transferred_by_name or None,
        "notes": row.notes or "",
        "transferred_at": iso(row.transferred_at),
        "resumed_at": iso(row.resumed_at),
        "resumed_by": opt(row.resumed_by),
        "resumed_by_name": resumed_by_name or None,
        "sla_response_deadline": iso(row.sla_response_deadline),
        "sla_resolution_deadline": iso(row.sla_resolution_deadline),
        "sla_breached": bool(row.sla_breached),
        "sla_breached_at": iso(row.sla_breached_at),
        "escalation_level": row.escalation_level or 0,
        "escalated_at": iso(row.escalated_at),
        "picked_up_at": iso(row.picked_up_at),
        "expires_at": iso(row.expires_at),
    }


def broadcast_transfer(event: str, transfer: AgentTransfer, extra=None):
    payload = {
        "id": str(transfer.id),
        "contact_id": str(transfer.contact_id),
        "phone_number": transfer.phone_number,
        "whatsapp_account": transfer.whatsapp_account,
        "status": transfer.status,
        "source": transfer.source,
        "notes": transfer.notes or "",
        "transferred_at": iso(transfer.transferred_at),
    }
    if extra:
        payload.update(extra)
    if transfer.agent_id:
        payload["agent_id"] = str(transfer.agent_id)
    if transfer.team_id:
        payload["team_id"] = str(transfer.team_id)
    broadcast_org(transfer.organization_id, event, payload)


def save_and_finalize(transfer: AgentTransfer, account, contact, settings, end_session=False):
    set_sla_deadlines(transfer, settings)
    if transfer.agent_id:
        mark_pickup(transfer)
    transfer.save()
    if transfer.agent_id and settings and settings.assign_to_same_agent and not contact.assigned_user_id:
        contact.assigned_user_id = transfer.agent_id
        contact.save(update_fields=["assigned_user_id", "updated_at"])
    if end_session:
        ChatbotSession.objects.filter(
            organization_id=account.organization_id, contact=contact, status="active"
        ).update(status="cancelled", completed_at=dj_tz.now(), updated_at=dj_tz.now())
    extra = {"contact_name": contact.profile_name or ""}
    broadcast_transfer("agent_transfer", transfer, extra)
    dispatch_webhook(
        account.organization_id,
        EVENT_TRANSFER_CREATED,
        {
            "transfer_id": str(transfer.id),
            "contact_id": str(contact.id),
            "contact_phone": contact.phone_number,
            "contact_name": contact.profile_name or "",
            "source": transfer.source,
            "agent_id": str(transfer.agent_id) if transfer.agent_id else None,
            "whatsapp_account": transfer.whatsapp_account,
        },
    )


def create_transfer_to_queue(account, contact, source: str):
    if has_active_transfer(account.organization_id, contact.id):
        return
    settings = load_settings(account.organization_id, account.name)
    from apps.chatbot.processor import is_within_business_hours

    if settings and settings.business_hours_enabled and settings.business_hours:
        if not is_within_business_hours(settings.business_hours):
            if settings.out_of_hours_message:
                from apps.messaging.outbound import send_and_save_text

                send_and_save_text(account, contact, settings.out_of_hours_message)
            return
    transfer = AgentTransfer(
        organization_id=account.organization_id,
        contact=contact,
        whatsapp_account=account.name,
        phone_number=contact.phone_number,
        status="active",
        source=source,
        transferred_at=dj_tz.now(),
    )
    save_and_finalize(transfer, account, contact, settings, end_session=False)


def create_transfer_from_keyword(account, contact):
    create_transfer_to_queue(account, contact, "keyword")


def create_transfer_to_team(account, contact, team_id, notes: str, source: str):
    if has_active_transfer(account.organization_id, contact.id):
        return
    settings = load_settings(account.organization_id, account.name)
    team = Team.objects.filter(id=team_id, organization_id=account.organization_id, is_active=True).first()
    agent_id = assign_to_team(team, account.organization_id) if team else None
    transfer = AgentTransfer(
        organization_id=account.organization_id,
        contact=contact,
        whatsapp_account=account.name,
        phone_number=contact.phone_number,
        status="active",
        source=source,
        team=team,
        agent_id=agent_id,
        notes=notes or "",
        transferred_at=dj_tz.now(),
    )
    save_and_finalize(transfer, account, contact, settings, end_session=True)


def _user_team_ids(user_id):
    return list(TeamMember.objects.filter(user_id=user_id).values_list("team_id", flat=True))


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def transfers_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        return _list_transfers(request, oid)
    data = request_body(request)
    contact_id = data.get("contact_id")
    if not contact_id:
        return error("contact_id is required", http_status=400)
    contact = Contact.objects.filter(id=contact_id, organization_id=oid).first()
    if not contact:
        return error("Contact not found", http_status=404)
    if has_active_transfer(oid, contact.id):
        return error("Contact already has an active transfer", http_status=409)
    settings = load_settings(oid, data.get("whatsapp_account") or "")
    team = None
    team_id = data.get("team_id")
    if team_id:
        team = Team.objects.filter(id=team_id, organization_id=oid, is_active=True).first()
        if not team:
            return error("Team not found or inactive", http_status=400)
    agent_id = None
    if data.get("agent_id"):
        agent = User.objects.filter(id=data["agent_id"], organization_id=oid).first()
        if not agent:
            return error("Agent not found", http_status=404)
        if not agent.is_available:
            return error("Agent is currently away", http_status=400)
        agent_id = agent.id
    elif team:
        agent_id = assign_to_team(team, oid)
    elif settings and settings.assign_to_same_agent and contact.assigned_user_id:
        assigned = User.objects.filter(id=contact.assigned_user_id).first()
        if assigned and assigned.is_available:
            agent_id = assigned.id
    account_name = data.get("whatsapp_account") or contact.whatsapp_account or ""
    account = WhatsAppAccount.objects.filter(organization_id=oid, name=account_name).first()
    if account is None:
        account = WhatsAppAccount.objects.filter(organization_id=oid, status="active").first()
    transfer = AgentTransfer(
        organization_id=oid,
        contact=contact,
        whatsapp_account=account_name,
        phone_number=contact.phone_number,
        status="active",
        source=data.get("source") or "manual",
        agent_id=agent_id,
        team=team,
        transferred_by_user=request.user,
        notes=data.get("notes") or "",
        transferred_at=dj_tz.now(),
    )
    save_and_finalize(transfer, account or type("A", (), {"organization_id": oid, "name": account_name})(), contact, settings, end_session=True)
    return success(transfer_payload(transfer, contact_name=contact.profile_name or ""), http_status=201)


def _list_transfers(request, oid):
    has_full = request.user.has_permission("transfers", "write") or getattr(request.user, "is_super_admin", False)
    status = request.query_params.get("status") or ""
    team_id = request.query_params.get("team_id") or ""
    try:
        limit = int(request.query_params.get("limit") or 100)
    except ValueError:
        limit = 100
    try:
        offset = int(request.query_params.get("offset") or 0)
    except ValueError:
        offset = 0
    limit = min(max(limit, 1), 100)
    qs = AgentTransfer.objects.filter(organization_id=oid)
    if status:
        qs = qs.filter(status=status)
    if team_id == "general":
        qs = qs.filter(team_id__isnull=True)
    elif team_id:
        qs = qs.filter(team_id=team_id)
    user_teams = _user_team_ids(request.user.id)
    if not has_full:
        if user_teams:
            qs = qs.filter(Q(agent_id=request.user.id) | Q(agent_id__isnull=True, team_id__isnull=True) | Q(agent_id__isnull=True, team_id__in=user_teams))
        else:
            qs = qs.filter(Q(agent_id=request.user.id) | Q(agent_id__isnull=True, team_id__isnull=True))
    total = qs.count()
    if status == "resumed":
        qs = qs.order_by("-resumed_at", "-transferred_at")
    else:
        qs = qs.order_by("transferred_at")
    rows = list(qs.select_related("contact", "agent", "team", "transferred_by_user")[offset : offset + limit])
    general_queue = AgentTransfer.objects.filter(
        organization_id=oid, status="active", agent_id__isnull=True, team_id__isnull=True
    ).count()
    team_qs = AgentTransfer.objects.filter(
        organization_id=oid, status="active", agent_id__isnull=True, team_id__isnull=False
    )
    if not has_full:
        team_qs = team_qs.filter(team_id__in=user_teams) if user_teams else team_qs.none()
    team_counts = {str(r["team_id"]): r["c"] for r in team_qs.values("team_id").annotate(c=Count("id"))}
    items = []
    for row in rows:
        items.append(
            transfer_payload(
                row,
                contact_name=(row.contact.profile_name if row.contact else "") or "",
                agent_name=(row.agent.full_name if row.agent else "") or "",
                team_name=(row.team.name if row.team else "") or "",
                transferred_by_name=(row.transferred_by_user.full_name if row.transferred_by_user else "") or "",
            )
        )
    return success(
        {
            "transfers": items,
            "general_queue_count": general_queue,
            "team_queue_counts": team_counts,
            "total_count": total,
            "limit": limit,
            "offset": offset,
        }
    )


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def pick_next_transfer(request):
    oid = org_id(request)
    has_full = request.user.has_permission("transfers", "write") or getattr(request.user, "is_super_admin", False)
    has_pickup = request.user.has_permission("transfers", "pickup")
    settings = load_settings(oid, "")
    if not has_full and settings and not settings.allow_agent_queue_pickup:
        return error("Queue pickup is not allowed", http_status=403)
    if not has_full and not has_pickup:
        return error("You don't have permission to pick up transfers", http_status=403)
    team_id = request.query_params.get("team_id") or ""
    user_teams = _user_team_ids(request.user.id)
    with transaction.atomic():
        qs = AgentTransfer.objects.select_for_update(skip_locked=True).filter(
            organization_id=oid, status="active", agent_id__isnull=True
        )
        if team_id == "general":
            qs = qs.filter(team_id__isnull=True)
        elif team_id:
            try:
                parsed = uuid.UUID(str(team_id))
            except ValueError:
                return error("Invalid team_id", http_status=400)
            if not has_full and parsed not in user_teams:
                return error("Not a member of this team", http_status=403)
            qs = qs.filter(team_id=parsed)
        elif not has_full:
            if user_teams:
                qs = qs.filter(Q(team_id__isnull=True) | Q(team_id__in=user_teams))
            else:
                qs = qs.filter(team_id__isnull=True)
        transfer = qs.order_by("transferred_at").first()
        if not transfer:
            return error("No transfers in queue", http_status=404)
        transfer.agent_id = request.user.id
        mark_pickup(transfer)
        transfer.save()
    settings = load_settings(oid, transfer.whatsapp_account)
    contact = transfer.contact
    if settings and settings.assign_to_same_agent and contact and not contact.assigned_user_id:
        contact.assigned_user_id = request.user.id
        contact.save(update_fields=["assigned_user_id", "updated_at"])
    broadcast_transfer("agent_transfer_assign", transfer)
    dispatch_webhook(
        oid,
        EVENT_TRANSFER_ASSIGNED,
        {
            "transfer_id": str(transfer.id),
            "contact_id": str(transfer.contact_id),
            "source": transfer.source,
            "agent_id": str(request.user.id),
            "whatsapp_account": transfer.whatsapp_account,
        },
    )
    return success({"message": "Transfer picked successfully", "transfer": transfer_payload(transfer)})


@api_view(["PUT"])
@permission_classes([CookieAuthenticated])
def resume_transfer(request, transfer_id):
    oid = org_id(request)
    transfer = AgentTransfer.objects.filter(id=transfer_id, organization_id=oid).first()
    if not transfer:
        return error("Transfer not found", http_status=404)
    if transfer.status != "active":
        return error("Transfer is not active", http_status=400)
    transfer.status = "resumed"
    transfer.resumed_at = dj_tz.now()
    transfer.resumed_by = request.user.id
    transfer.save()
    broadcast_transfer("agent_transfer_resume", transfer)
    dispatch_webhook(
        oid,
        EVENT_TRANSFER_RESUMED,
        {
            "transfer_id": str(transfer.id),
            "contact_id": str(transfer.contact_id),
            "source": transfer.source,
            "whatsapp_account": transfer.whatsapp_account,
        },
    )
    return success({"message": "Chatbot resumed", "status": "resumed"})


@api_view(["PUT"])
@permission_classes([CookieAuthenticated])
def assign_transfer(request, transfer_id):
    oid = org_id(request)
    transfer = AgentTransfer.objects.filter(id=transfer_id, organization_id=oid).first()
    if not transfer:
        return error("Transfer not found", http_status=404)
    if transfer.status != "active":
        return error("Transfer is not active", http_status=400)
    data = request_body(request)
    has_full = request.user.has_permission("transfers", "write") or getattr(request.user, "is_super_admin", False)
    agent_id = data.get("agent_id")
    team_id = data.get("team_id")
    if team_id:
        team = Team.objects.filter(id=team_id, organization_id=oid, is_active=True).first()
        if not team:
            return error("Team not found or inactive", http_status=400)
        transfer.team = team
    if agent_id:
        if not has_full and str(agent_id) != str(request.user.id):
            return error("You can only assign transfers to yourself", http_status=403)
        agent = User.objects.filter(id=agent_id, organization_id=oid).first()
        if not agent:
            return error("Agent not found", http_status=404)
        transfer.agent_id = agent.id
        mark_pickup(transfer)
    else:
        transfer.agent_id = None
        transfer.picked_up_at = None
    transfer.save()
    broadcast_transfer("agent_transfer_assign", transfer)
    dispatch_webhook(
        oid,
        EVENT_TRANSFER_ASSIGNED,
        {
            "transfer_id": str(transfer.id),
            "contact_id": str(transfer.contact_id),
            "source": transfer.source,
            "agent_id": str(transfer.agent_id) if transfer.agent_id else None,
            "whatsapp_account": transfer.whatsapp_account,
        },
    )
    return success({"message": "Transfer assigned successfully", "agent_id": str(transfer.agent_id) if transfer.agent_id else None})
