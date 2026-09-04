import secrets

import bcrypt
from django.utils import timezone as dj_tz
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import APIKey
from apps.common.envelope import error, success
from apps.common.http import iso, org_id, parse_pagination, require_perm
from apps.common.permissions import CookieAuthenticated


def _key_payload(key: APIKey, *, include_secret=None):
    payload = {
        "id": str(key.id),
        "name": key.name,
        "key_prefix": key.key_prefix,
        "last_used_at": iso(key.last_used_at),
        "expires_at": iso(key.expires_at),
        "is_active": key.is_active,
        "created_at": iso(key.created_at),
    }
    if include_secret:
        payload["key"] = include_secret
    return payload


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def api_keys_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        require_perm(request, "api_keys", "read")
        page, limit, offset = parse_pagination(request)
        qs = APIKey.objects.filter(organization_id=oid)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        total = qs.count()
        items = [_key_payload(k) for k in qs.order_by("-created_at")[offset : offset + limit]]
        return success({"api_keys": items, "total": total, "page": page, "limit": limit})

    require_perm(request, "api_keys", "write")
    data = request.data if isinstance(request.data, dict) else {}
    name = (data.get("name") or "").strip()
    if not name:
        return error("Name is required", http_status=400)
    expires_at = None
    if data.get("expires_at"):
        expires_at = data["expires_at"]
    raw = "whm_" + secrets.token_hex(16)
    prefix = raw[4:20]
    hashed = bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")
    key = APIKey.objects.create(
        organization_id=oid,
        user=request.user,
        name=name,
        key_prefix=prefix,
        key_hash=hashed,
        expires_at=expires_at,
        is_active=True,
    )
    return success(_key_payload(key, include_secret=raw), http_status=201)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def api_key_detail(request, key_id):
    oid = org_id(request)
    try:
        key = APIKey.objects.get(id=key_id, organization_id=oid)
    except APIKey.DoesNotExist:
        return error("API key not found", http_status=404)
    if request.method == "GET":
        require_perm(request, "api_keys", "read")
        return success(_key_payload(key))
    if request.method == "DELETE":
        require_perm(request, "api_keys", "delete")
        key.deleted_at = dj_tz.now()
        key.save(update_fields=["deleted_at"])
        return success({"message": "API key deleted"})
    require_perm(request, "api_keys", "write")
    data = request.data if isinstance(request.data, dict) else {}
    if "is_active" in data:
        key.is_active = bool(data["is_active"])
        key.save(update_fields=["is_active", "updated_at"])
    return success(_key_payload(key))
