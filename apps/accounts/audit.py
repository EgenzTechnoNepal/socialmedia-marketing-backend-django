from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import AuditLog
from apps.common.envelope import error, success
from apps.common.http import iso, org_id, parse_pagination, require_perm
from apps.common.permissions import CookieAuthenticated


def _log_payload(row: AuditLog):
    return {
        "id": str(row.id),
        "resource_type": row.resource_type,
        "resource_id": str(row.resource_id),
        "user_id": str(row.user_id),
        "user_name": row.user_name or "",
        "action": row.action,
        "changes": row.changes or [],
        "created_at": iso(row.created_at),
    }


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def audit_logs_collection(request):
    oid = org_id(request)
    require_perm(request, "audit_logs", "read")
    page, limit, offset = parse_pagination(request)
    qs = AuditLog.objects.filter(organization_id=oid)
    if request.query_params.get("resource_type"):
        qs = qs.filter(resource_type=request.query_params["resource_type"])
    if request.query_params.get("resource_id"):
        qs = qs.filter(resource_id=request.query_params["resource_id"])
    if request.query_params.get("user_id"):
        qs = qs.filter(user_id=request.query_params["user_id"])
    if request.query_params.get("action"):
        qs = qs.filter(action=request.query_params["action"])
    if request.query_params.get("from"):
        qs = qs.filter(created_at__gte=request.query_params["from"])
    if request.query_params.get("to"):
        qs = qs.filter(created_at__lte=request.query_params["to"])
    total = qs.count()
    items = [_log_payload(row) for row in qs.order_by("-created_at")[offset : offset + limit]]
    return success({"audit_logs": items, "total": total, "page": page, "limit": limit})


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def audit_log_detail(request, log_id):
    oid = org_id(request)
    require_perm(request, "audit_logs", "read")
    try:
        row = AuditLog.objects.get(id=log_id, organization_id=oid)
    except AuditLog.DoesNotExist:
        return error("Audit log not found", http_status=404)
    return success(_log_payload(row))
