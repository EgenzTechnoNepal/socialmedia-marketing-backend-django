from datetime import datetime, timezone

from django.db.models import Count, Q
from django.db.models.functions import TruncDay, TruncWeek
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import User, UserAvailabilityLog
from apps.chatbot.models import AgentTransfer, TeamMember
from apps.common.envelope import error, success
from apps.common.exceptions import APIError
from apps.common.http import iso, is_super, org_id, period_bounds, require_perm
from apps.common.permissions import CookieAuthenticated
from apps.messaging.models import Message


def calculate_break_time(agent_id, start, end):
    now = datetime.now(timezone.utc)
    logs = UserAvailabilityLog.objects.filter(
        user_id=agent_id,
        is_available=False,
        started_at__lte=end,
    ).filter(Q(ended_at__gte=start) | Q(ended_at__isnull=True))
    total = 0.0
    count = 0
    for log in logs:
        log_start = log.started_at if log.started_at > start else start
        log_end = log.ended_at or now
        if log_end > end:
            log_end = end
        if log_end > log_start:
            total += (log_end - log_start).total_seconds() / 60.0
            count += 1
    return total, count


def calculate_agent_stats(oid, agent_id, start, end) -> dict:
    stats = {
        "agent_id": str(agent_id),
        "agent_name": "",
        "avg_first_response_mins": 0.0,
        "avg_resolution_mins": 0.0,
        "transfers_handled": 0,
        "active_transfers": 0,
        "messages_sent": 0,
        "total_break_time_mins": 0.0,
        "break_count": 0,
        "is_available": True,
    }
    agent = User.objects.filter(id=agent_id).first()
    if agent:
        stats["agent_name"] = agent.full_name or ""
        stats["is_available"] = bool(agent.is_available)
    stats["transfers_handled"] = AgentTransfer.objects.filter(
        organization_id=oid, agent_id=agent_id, status="resumed", transferred_at__gte=start, transferred_at__lte=end
    ).count()
    stats["active_transfers"] = AgentTransfer.objects.filter(
        organization_id=oid, agent_id=agent_id, status="active"
    ).count()
    contact_ids = AgentTransfer.objects.filter(agent_id=agent_id, organization_id=oid).values("contact_id")
    stats["messages_sent"] = Message.objects.filter(
        organization_id=oid,
        direction="outgoing",
        created_at__gte=start,
        created_at__lte=end,
        contact_id__in=contact_ids,
    ).count()
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(AVG(EXTRACT(EPOCH FROM (resumed_at - transferred_at))/60), 0)
            FROM agent_transfers
            WHERE organization_id = %s AND agent_id = %s AND status = 'resumed'
              AND resumed_at IS NOT NULL AND transferred_at >= %s AND transferred_at <= %s
            """,
            [str(oid), str(agent_id), start, end],
        )
        stats["avg_resolution_mins"] = float(cursor.fetchone()[0] or 0)
    stats["total_break_time_mins"], stats["break_count"] = calculate_break_time(agent_id, start, end)
    if not stats["is_available"]:
        current = (
            UserAvailabilityLog.objects.filter(user_id=agent_id, is_available=False, ended_at__isnull=True)
            .order_by("-started_at")
            .first()
        )
        if current:
            stats["current_break_start"] = iso(current.started_at)
    return stats


def calculate_trend(oid, start, end, group_by, agent_id=None):
    trunc = TruncWeek("transferred_at") if group_by == "week" else TruncDay("transferred_at")
    qs = AgentTransfer.objects.filter(
        organization_id=oid, status="resumed", transferred_at__gte=start, transferred_at__lte=end
    )
    if agent_id:
        qs = qs.filter(agent_id=agent_id)
    rows = qs.annotate(date=trunc).values("date").annotate(count=Count("id")).order_by("date")
    trend = []
    for row in rows:
        date = row["date"]
        trend.append(
            {
                "date": date.strftime("%Y-%m-%d") if date else "",
                "transfers_handled": row["count"],
            }
        )
    return trend


def _source_counts(qs):
    result = {}
    for row in qs.values("source").annotate(count=Count("id")):
        result[row["source"] or ""] = row["count"]
    return result


def calculate_summary(oid, start, end, agent_id=None) -> dict:
    base = AgentTransfer.objects.filter(organization_id=oid, transferred_at__gte=start, transferred_at__lte=end)
    if agent_id:
        base = base.filter(agent_id=agent_id)
    handled = base.filter(status="resumed")
    active_qs = AgentTransfer.objects.filter(organization_id=oid, status="active")
    if agent_id:
        active_qs = active_qs.filter(agent_id=agent_id)
    from django.db import connection

    if agent_id:
        sql_queue = """
            SELECT COALESCE(AVG(EXTRACT(EPOCH FROM (updated_at - transferred_at))/60), 0)
            FROM agent_transfers
            WHERE organization_id = %s AND agent_id = %s AND agent_id IS NOT NULL
              AND transferred_at >= %s AND transferred_at <= %s
        """
        sql_res = """
            SELECT COALESCE(AVG(EXTRACT(EPOCH FROM (resumed_at - transferred_at))/60), 0)
            FROM agent_transfers
            WHERE organization_id = %s AND agent_id = %s AND status = 'resumed'
              AND resumed_at IS NOT NULL AND transferred_at >= %s AND transferred_at <= %s
        """
        with connection.cursor() as cursor:
            cursor.execute(sql_queue, [str(oid), str(agent_id), start, end])
            queue = float(cursor.fetchone()[0] or 0)
            cursor.execute(sql_res, [str(oid), str(agent_id), start, end])
            resolution = float(cursor.fetchone()[0] or 0)
        break_mins, break_count = calculate_break_time(agent_id, start, end)
    else:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(AVG(EXTRACT(EPOCH FROM (updated_at - transferred_at))/60), 0)
                FROM agent_transfers
                WHERE organization_id = %s AND agent_id IS NOT NULL
                  AND transferred_at >= %s AND transferred_at <= %s
                """,
                [str(oid), start, end],
            )
            queue = float(cursor.fetchone()[0] or 0)
            cursor.execute(
                """
                SELECT COALESCE(AVG(EXTRACT(EPOCH FROM (resumed_at - transferred_at))/60), 0)
                FROM agent_transfers
                WHERE organization_id = %s AND status = 'resumed' AND resumed_at IS NOT NULL
                  AND transferred_at >= %s AND transferred_at <= %s
                """,
                [str(oid), start, end],
            )
            resolution = float(cursor.fetchone()[0] or 0)
        break_mins, break_count = 0.0, 0
    return {
        "total_transfers_handled": handled.count(),
        "active_transfers": active_qs.count(),
        "avg_queue_time_mins": queue,
        "avg_first_response_mins": 0.0,
        "avg_resolution_mins": resolution,
        "transfers_by_source": _source_counts(base),
        "total_break_time_mins": break_mins,
        "break_count": break_count,
    }


def _org_agents(oid):
    user_ids = TeamMember.objects.filter(role="agent", team__organization_id=oid).values_list("user_id", flat=True)
    return list(User.objects.filter(id__in=user_ids, organization_id=oid))


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def agent_analytics(request):
    oid = org_id(request)
    start, end, err = period_bounds(request)
    if err:
        return error(err, http_status=400)
    group_by = request.query_params.get("group_by") or "day"
    agent_id_str = request.query_params.get("agent_id") or ""
    can_read = is_super(request) or request.user.has_permission("analytics", "read")
    response = {"summary": {"transfers_by_source": {}}, "trend_data": [], "agent_stats": None, "my_stats": None}
    filter_id = None
    if can_read and agent_id_str:
        filter_id = agent_id_str
    if filter_id:
        stats = calculate_agent_stats(oid, filter_id, start, end)
        response["my_stats"] = stats
        response["trend_data"] = calculate_trend(oid, start, end, group_by, filter_id)
        response["summary"] = calculate_summary(oid, start, end, filter_id)
    elif not can_read:
        stats = calculate_agent_stats(oid, request.user.id, start, end)
        response["my_stats"] = stats
        response["trend_data"] = calculate_trend(oid, start, end, group_by, request.user.id)
        response["summary"] = calculate_summary(oid, start, end, request.user.id)
    else:
        response["summary"] = calculate_summary(oid, start, end)
        response["trend_data"] = calculate_trend(oid, start, end, group_by)
        response["agent_stats"] = [calculate_agent_stats(oid, agent.id, start, end) for agent in _org_agents(oid)]
        response["my_stats"] = calculate_agent_stats(oid, request.user.id, start, end)
    return success(response)


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def agent_details(request, agent_id):
    oid = org_id(request)
    require_perm(request, "analytics", "read")
    start, end, err = period_bounds(request)
    if err:
        start, end, _ = period_bounds(type("R", (), {"query_params": {}})())
    if not User.objects.filter(id=agent_id, organization_id=oid).exists():
        raise APIError("Agent not found", status_code=404)
    group_by = request.query_params.get("group_by") or "day"
    return success(
        {
            "agent": calculate_agent_stats(oid, agent_id, start, end),
            "trend_data": calculate_trend(oid, start, end, group_by, agent_id),
        }
    )


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def agent_comparison(request):
    oid = org_id(request)
    require_perm(request, "analytics", "read")
    start, end, err = period_bounds(request)
    if err:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        end = now
    return success({"agents": [calculate_agent_stats(oid, agent.id, start, end) for agent in _org_agents(oid)]})
