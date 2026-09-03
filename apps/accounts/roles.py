import re
import uuid

from django.db import connection, transaction
from django.db.models import Count, Q
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny

from apps.accounts.models import CustomRole, Permission, UserOrganization
from apps.common.envelope import error, success
from apps.common.http import list_payload, org_id, parse_pagination, user_iso
from apps.common.permissions import CookieAuthenticated


def _permission_keys(role_id) -> list[str]:
    keys = []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT p.resource, p.action
            FROM role_permissions rp
            JOIN permissions p ON p.id = rp.permission_id
            WHERE rp.custom_role_id = %s AND p.deleted_at IS NULL
            """,
            [str(role_id)],
        )
        keys = [f"{resource}:{action}" for resource, action in cursor.fetchall()]
    return keys


def _set_role_permissions(role_id, keys: list[str]):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM role_permissions WHERE custom_role_id = %s", [str(role_id)])
        for key in keys or []:
            if ":" not in key:
                continue
            resource, action = key.rsplit(":", 1)
            cursor.execute(
                """
                INSERT INTO role_permissions (custom_role_id, permission_id)
                SELECT %s, id FROM permissions
                WHERE resource = %s AND action = %s AND deleted_at IS NULL
                """,
                [str(role_id), resource, action],
            )


def _role_payload(role) -> dict:
    user_count = UserOrganization.objects.filter(role_id=role.id).count()
    return {
        "id": str(role.id),
        "name": role.name,
        "description": role.description or "",
        "is_system": role.is_system,
        "is_default": role.is_default,
        "permissions": _permission_keys(role.id),
        "user_count": user_count,
        "created_at": user_iso(role.created_at),
        "updated_at": user_iso(role.updated_at),
    }


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def roles_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        page, limit, offset = parse_pagination(request)
        qs = CustomRole.objects.filter(organization_id=oid)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        total = qs.count()
        roles = list(qs.order_by("name")[offset : offset + limit])
        return success(list_payload("roles", [_role_payload(r) for r in roles], total, page, limit))

    data = request.data if isinstance(request.data, dict) else {}
    name = (data.get("name") or "").strip()
    if not name:
        return error("name is required", http_status=400)
    if data.get("is_default"):
        CustomRole.objects.filter(organization_id=oid, is_default=True).update(is_default=False)
    role = CustomRole.objects.create(
        organization_id=oid,
        name=name,
        description=data.get("description") or "",
        is_default=bool(data.get("is_default")),
        is_system=False,
    )
    _set_role_permissions(role.id, data.get("permissions") or [])
    return success(_role_payload(role), http_status=201)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def role_detail(request, role_id):
    oid = org_id(request)
    try:
        role = CustomRole.objects.get(id=role_id, organization_id=oid)
    except CustomRole.DoesNotExist:
        return error("Role not found", http_status=404)
    if request.method == "GET":
        return success(_role_payload(role))
    if request.method == "DELETE":
        if role.is_system:
            return error("System roles cannot be deleted", http_status=400)
        if UserOrganization.objects.filter(role_id=role.id).exists():
            return error("Role is assigned to users", http_status=409)
        role.deleted_at = role.updated_at
        from django.utils import timezone as dj_tz

        role.deleted_at = dj_tz.now()
        role.save(update_fields=["deleted_at"])
        return success({"message": "Role deleted"})
    data = request.data if isinstance(request.data, dict) else {}
    if role.is_system and data.get("name") and data.get("name") != role.name:
        return error("System role names cannot be changed", http_status=400)
    if "name" in data and data.get("name"):
        role.name = data["name"]
    if "description" in data:
        role.description = data.get("description") or ""
    if data.get("is_default"):
        CustomRole.objects.filter(organization_id=oid, is_default=True).exclude(id=role.id).update(is_default=False)
        role.is_default = True
    elif "is_default" in data:
        role.is_default = bool(data.get("is_default"))
    role.save()
    if "permissions" in data:
        _set_role_permissions(role.id, data.get("permissions") or [])
    return success(_role_payload(role))


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def list_permissions(request):
    perms = Permission.objects.all().order_by("resource", "action")
    return success(
        {
            "permissions": [
                {
                    "id": str(p.id),
                    "resource": p.resource,
                    "action": p.action,
                    "description": p.description or "",
                    "key": f"{p.resource}:{p.action}",
                }
                for p in perms
            ]
        }
    )


SYSTEM_ROLE_PERMS = {
    "manager": [
        "teams:read",
        "settings.general:read",
        "settings.general:write",
        "settings.chatbot:read",
        "settings.chatbot:write",
        "settings.billing:read",
        "settings.billing:write",
        "accounts:read",
        "accounts:write",
        "accounts:delete",
        "templates:read",
        "templates:write",
        "templates:delete",
        "templates:sync",
        "chat:read",
        "chat:write",
        "chat.assign:write",
        "contacts:read",
        "contacts:write",
        "contacts:delete",
        "contacts:import",
        "contacts:export",
        "tags:read",
        "tags:write",
        "tags:delete",
        "canned_responses:read",
        "canned_responses:write",
        "canned_responses:delete",
        "organizations:read",
    ],
    "agent": [
        "accounts:read",
        "chat:read",
        "chat:write",
        "contacts:read",
        "tags:read",
        "canned_responses:read",
    ],
}


def seed_system_roles(organization):
    catalog = list(Permission.objects.all())
    all_keys = [f"{p.resource}:{p.action}" for p in catalog]
    specs = [
        ("admin", True, False, all_keys),
        ("manager", True, False, SYSTEM_ROLE_PERMS["manager"]),
        ("agent", True, True, SYSTEM_ROLE_PERMS["agent"]),
    ]
    for name, is_system, is_default, keys in specs:
        role = CustomRole.objects.create(
            organization=organization,
            name=name,
            description=f"{name} role",
            is_system=is_system,
            is_default=is_default,
        )
        _set_role_permissions(role.id, keys)
    return CustomRole.objects.get(organization=organization, name="admin", is_system=True)
