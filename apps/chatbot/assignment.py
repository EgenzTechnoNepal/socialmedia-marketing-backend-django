from datetime import timedelta

from django.db.models import Count
from django.utils import timezone as dj_tz

from apps.accounts.models import User
from apps.chatbot.models import AgentTransfer, Team, TeamMember


def assign_to_team(team: Team, org_id, exclude=None):
    exclude = set(str(x) for x in (exclude or []))
    members = list(TeamMember.objects.filter(team=team).select_related("user"))
    available = []
    for member in members:
        user = member.user
        if not user or not user.is_available or not user.is_active:
            continue
        if str(user.id) in exclude:
            continue
        available.append(member)
    if not available:
        return None
    strategy = team.assignment_strategy or "round_robin"
    if strategy == "manual":
        return None
    if strategy == "load_balanced":
        ids = [m.user_id for m in available]
        counts = {
            row["agent_id"]: row["c"]
            for row in AgentTransfer.objects.filter(
                organization_id=org_id, status="active", agent_id__in=ids
            ).values("agent_id").annotate(c=Count("id"))
        }
        best = min(available, key=lambda m: counts.get(m.user_id, 0))
        return best.user_id
    available.sort(key=lambda m: (m.last_assigned_at is not None, m.last_assigned_at or dj_tz.now()))
    chosen = available[0]
    chosen.last_assigned_at = dj_tz.now()
    chosen.save(update_fields=["last_assigned_at", "updated_at"])
    return chosen.user_id


def set_sla_deadlines(transfer: AgentTransfer, settings):
    if not settings or not settings.sla_enabled:
        return
    now = dj_tz.now()
    if settings.sla_response_minutes:
        transfer.sla_response_deadline = now + timedelta(minutes=settings.sla_response_minutes)
    if settings.sla_resolution_minutes:
        transfer.sla_resolution_deadline = now + timedelta(minutes=settings.sla_resolution_minutes)
    if settings.sla_escalation_minutes:
        transfer.sla_escalation_at = now + timedelta(minutes=settings.sla_escalation_minutes)
    if settings.sla_auto_close_hours:
        transfer.expires_at = now + timedelta(hours=settings.sla_auto_close_hours)


def mark_pickup(transfer: AgentTransfer):
    now = dj_tz.now()
    transfer.picked_up_at = now
    if transfer.sla_response_deadline and now > transfer.sla_response_deadline:
        transfer.sla_breached = True
        transfer.sla_breached_at = now
