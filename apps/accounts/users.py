import bcrypt
from django.db.models import Q
from django.utils import timezone as dj_tz
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import CustomRole, Organization, User, UserOrganization
from apps.accounts.payloads import apply_org_role, user_to_response
from apps.billing.entitlements import assert_can_add_seat
from apps.common.envelope import error, success
from apps.common.exceptions import APIError
from apps.common.http import is_super, list_payload, org_id, parse_pagination, require_perm
from apps.common.permissions import CookieAuthenticated
from apps.realtime.hub import online_user_ids


def _org_users_qs(oid):
    member_ids = UserOrganization.objects.filter(organization_id=oid).values("user_id")
    return User.objects.filter(id__in=member_ids)


def _user_payload(user, oid):
    home = str(user.organization_id) if user.organization_id else ""
    apply_org_role(user, oid)
    return user_to_response(user, is_member=home != str(oid))


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def users_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        require_perm(request, "users", "read")
        page, limit, offset = parse_pagination(request)
        qs = _org_users_qs(oid)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(full_name__icontains=search) | Q(email__icontains=search))
        role_id = request.query_params.get("role_id")
        if role_id:
            member_ids = UserOrganization.objects.filter(organization_id=oid, role_id=role_id).values("user_id")
            qs = qs.filter(id__in=member_ids)
        online_ids = set(online_user_ids(oid))
        if request.query_params.get("online_only") == "true":
            qs = qs.filter(id__in=list(online_ids) or [])
        total = qs.count()
        users = list(qs.order_by("full_name")[offset : offset + limit])
        payload = list_payload(
            "users",
            [_user_payload(u, oid) for u in users],
            total,
            page,
            limit,
            online_count=len(online_ids),
        )
        return success(payload)

    require_perm(request, "users", "write")
    assert_can_add_seat(oid)
    data = request.data if isinstance(request.data, dict) else {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    full_name = (data.get("full_name") or "").strip()
    if not email or not password or not full_name:
        return error("email, password, and full_name are required", http_status=400)
    role_id = data.get("role_id")
    if not role_id:
        role = (
            CustomRole.objects.filter(organization_id=oid, is_default=True).first()
            or CustomRole.objects.filter(organization_id=oid, name="agent", is_system=True).first()
        )
        role_id = role.id if role else None
    existing = User.objects.filter(email=email).first()
    if existing:
        if UserOrganization.objects.filter(user=existing, organization_id=oid).exists():
            return error("User already belongs to this organization", http_status=409)
        UserOrganization.objects.create(user=existing, organization_id=oid, role_id=role_id, is_default=False)
        return success(_user_payload(existing, oid), http_status=201)
    org = Organization.objects.get(id=oid)
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")
    is_super_admin = bool(data.get("is_super_admin")) if is_super(request) else False
    user = User.objects.create(
        organization=org,
        email=email,
        password_hash=password_hash,
        full_name=full_name,
        role_id=role_id,
        is_active=True,
        is_super_admin=is_super_admin,
    )
    UserOrganization.objects.create(user=user, organization=org, role_id=role_id, is_default=True)
    return success(_user_payload(user, oid), http_status=201)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def user_detail(request, user_id):
    oid = org_id(request)
    membership = UserOrganization.objects.filter(user_id=user_id, organization_id=oid).first()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return error("User not found", http_status=404)
    if not membership and str(user.organization_id) != str(oid) and not is_super(request):
        return error("User not found", http_status=404)

    if request.method == "GET":
        return success(_user_payload(user, oid))

    if request.method == "DELETE":
        require_perm(request, "users", "delete")
        home = str(user.organization_id) == str(oid)
        if not home and membership:
            membership.deleted_at = dj_tz.now()
            membership.save(update_fields=["deleted_at"])
            return success({"message": "Member removed from organization"})
        user.deleted_at = dj_tz.now()
        user.is_active = False
        user.save(update_fields=["deleted_at", "is_active", "updated_at"])
        return success({"message": "User deleted successfully"})

    self_update = str(request.user.id) == str(user_id)
    if not self_update:
        require_perm(request, "users", "write")
    data = request.data if isinstance(request.data, dict) else {}
    if "full_name" in data:
        user.full_name = data.get("full_name") or user.full_name
    if "email" in data and data.get("email"):
        user.email = data["email"].strip()
    if data.get("password"):
        user.password_hash = bcrypt.hashpw(str(data["password"]).encode("utf-8"), bcrypt.gensalt(rounds=10)).decode(
            "utf-8"
        )
    if "is_active" in data and not self_update:
        user.is_active = bool(data["is_active"])
    if "role_id" in data:
        require_perm(request, "users", "write")
        user.role_id = data.get("role_id")
        if membership:
            membership.role_id = data.get("role_id")
            membership.save(update_fields=["role_id", "updated_at"])
    if "is_super_admin" in data and is_super(request):
        user.is_super_admin = bool(data["is_super_admin"])
    user.save()
    return success(_user_payload(user, oid))
