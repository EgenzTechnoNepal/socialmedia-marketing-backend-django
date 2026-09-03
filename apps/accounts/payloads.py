from datetime import timezone

from django.db import connection

from apps.accounts.models import CustomRole, User, UserOrganization


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_role_payload(role_id):
    if not role_id:
        return None
    try:
        role = CustomRole.objects.get(id=role_id)
    except CustomRole.DoesNotExist:
        return None

    permissions = []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT p.id, p.resource, p.action, COALESCE(p.description, '')
            FROM role_permissions rp
            JOIN permissions p ON p.id = rp.permission_id
            WHERE rp.custom_role_id = %s
              AND p.deleted_at IS NULL
            """,
            [str(role_id)],
        )
        for perm_id, resource, action, description in cursor.fetchall():
            permissions.append(
                {
                    "id": str(perm_id),
                    "resource": resource,
                    "action": action,
                    "description": description,
                }
            )

    return {
        "id": str(role.id),
        "name": role.name,
        "description": role.description or "",
        "is_system": role.is_system,
        "permissions": permissions,
    }


def apply_org_role(user: User, organization_id) -> User:
    """Use membership role for the active org (same as Go GetCurrentUser / SwitchOrg)."""
    if not organization_id:
        return user
    user.organization_id = organization_id
    membership = (
        UserOrganization.objects.filter(user=user, organization_id=organization_id)
        .order_by("created_at")
        .first()
    )
    if membership and membership.role_id:
        user.role_id = membership.role_id
    return user


def user_to_response(user: User, is_member: bool = False) -> dict:
    role_id = str(user.role_id) if user.role_id else None
    payload = {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name or "",
        "role_id": role_id,
        "is_active": user.is_active,
        "is_available": user.is_available,
        "is_super_admin": user.is_super_admin,
        "organization_id": str(user.organization_id) if user.organization_id else None,
        "settings": user.settings or {},
        "created_at": _iso(user.created_at),
        "updated_at": _iso(user.updated_at),
        "is_member": is_member,
    }
    role = load_role_payload(user.role_id)
    if role:
        payload["role"] = role
    return payload
