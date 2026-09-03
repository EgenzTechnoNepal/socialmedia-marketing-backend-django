from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import User
from apps.chatbot.models import Team, TeamMember
from apps.common.envelope import error, success
from apps.common.http import iso, list_payload, org_id, parse_pagination, request_body, require_perm, soft_delete
from apps.common.permissions import CookieAuthenticated


def member_payload(member: TeamMember) -> dict:
    user = member.user
    return {
        "id": str(member.id),
        "user_id": str(member.user_id),
        "full_name": user.full_name if user else "",
        "email": user.email if user else "",
        "role": member.role,
        "is_available": bool(user.is_available) if user else False,
        "last_assigned_at": iso(member.last_assigned_at),
    }


def team_payload(team: Team, include_members=False) -> dict:
    members = list(team.members.all()) if include_members or hasattr(team, "_prefetched_objects_cache") else []
    if not include_members:
        members = list(getattr(team, "members").all()) if team.id else []
    payload = {
        "id": str(team.id),
        "name": team.name,
        "description": team.description or "",
        "assignment_strategy": team.assignment_strategy,
        "per_agent_timeout_secs": team.per_agent_timeout_secs or 0,
        "is_active": team.is_active,
        "member_count": len(members) if members else team.members.count(),
        "created_by_id": str(team.created_by_id) if team.created_by_id else None,
        "created_by_name": team.created_by.full_name if team.created_by else "",
        "updated_by_id": str(team.updated_by_id) if team.updated_by_id else None,
        "updated_by_name": team.updated_by.full_name if team.updated_by else "",
        "created_at": iso(team.created_at),
        "updated_at": iso(team.updated_at),
    }
    if include_members:
        payload["members"] = [member_payload(m) for m in members]
    return payload


def _user_team_ids(user_id):
    return list(TeamMember.objects.filter(user_id=user_id).values_list("team_id", flat=True))


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def teams_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        page, limit, offset = parse_pagination(request)
        qs = Team.objects.filter(organization_id=oid).prefetch_related("members", "members__user").select_related("created_by", "updated_by")
        search = request.query_params.get("search") or ""
        if search:
            qs = qs.filter(name__icontains=search)
        if not request.user.has_permission("teams", "read"):
            qs = qs.filter(id__in=_user_team_ids(request.user.id))
        total = qs.count()
        rows = list(qs.order_by("name")[offset : offset + limit])
        return success(list_payload("teams", [team_payload(t) for t in rows], total, page, limit))
    require_perm(request, "teams", "write")
    data = request_body(request)
    if not data.get("name"):
        return error("Team name is required", http_status=400)
    strategy = data.get("assignment_strategy") or "round_robin"
    if strategy not in {"round_robin", "load_balanced", "manual"}:
        return error("Invalid assignment strategy", http_status=400)
    team = Team.objects.create(
        organization_id=oid,
        name=data["name"],
        description=data.get("description") or "",
        assignment_strategy=strategy,
        per_agent_timeout_secs=data.get("per_agent_timeout_secs") or 0,
        is_active=True,
        created_by=request.user,
        updated_by=request.user,
    )
    team = Team.objects.select_related("created_by", "updated_by").prefetch_related("members").get(id=team.id)
    return success({"team": team_payload(team)})


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def team_detail(request, team_id):
    oid = org_id(request)
    team = (
        Team.objects.filter(id=team_id, organization_id=oid)
        .select_related("created_by", "updated_by")
        .prefetch_related("members", "members__user")
        .first()
    )
    if not team:
        return error("Team not found", http_status=404)
    if request.method == "GET":
        return success({"team": team_payload(team, include_members=True)})
    if request.method == "DELETE":
        require_perm(request, "teams", "delete")
        TeamMember.objects.filter(team=team).update(deleted_at=team.updated_at)
        soft_delete(team)
        return success({"message": "Team deleted successfully"})
    if not request.user.has_permission("teams", "write"):
        is_manager = TeamMember.objects.filter(team=team, user_id=request.user.id, role="manager").exists()
        if not is_manager:
            return error("Insufficient permissions", http_status=403)
    data = request_body(request)
    if data.get("name") is not None:
        team.name = data["name"]
    if data.get("description") is not None:
        team.description = data["description"]
    if data.get("assignment_strategy"):
        if data["assignment_strategy"] not in {"round_robin", "load_balanced", "manual"}:
            return error("Invalid assignment strategy", http_status=400)
        team.assignment_strategy = data["assignment_strategy"]
    if data.get("per_agent_timeout_secs") is not None:
        team.per_agent_timeout_secs = data["per_agent_timeout_secs"]
    if data.get("is_active") is not None:
        team.is_active = data["is_active"]
    team.updated_by = request.user
    team.save()
    return success({"team": team_payload(team, include_members=True)})


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def team_members(request, team_id):
    oid = org_id(request)
    team = Team.objects.filter(id=team_id, organization_id=oid).first()
    if not team:
        return error("Team not found", http_status=404)
    if request.method == "GET":
        rows = list(TeamMember.objects.filter(team=team).select_related("user"))
        return success({"members": [member_payload(m) for m in rows]})
    require_perm(request, "teams", "write")
    data = request_body(request)
    user_id = data.get("user_id")
    if not user_id:
        return error("user_id is required", http_status=400)
    user = User.objects.filter(id=user_id, organization_id=oid).first()
    if not user:
        return error("User not found", http_status=404)
    if TeamMember.objects.filter(team=team, user=user).exists():
        return error("User is already a member of this team", http_status=409)
    member = TeamMember.objects.create(team=team, user=user, role=data.get("role") or "agent")
    member = TeamMember.objects.select_related("user").get(id=member.id)
    return success({"member": member_payload(member)})


@api_view(["DELETE"])
@permission_classes([CookieAuthenticated])
def team_member_detail(request, team_id, member_user_id):
    oid = org_id(request)
    team = Team.objects.filter(id=team_id, organization_id=oid).first()
    if not team:
        return error("Team not found", http_status=404)
    require_perm(request, "teams", "write")
    member = TeamMember.objects.filter(team=team, user_id=member_user_id).first()
    if not member:
        return error("Member not found", http_status=404)
    soft_delete(member)
    return success({"message": "Member removed from team"})
