from datetime import datetime, timedelta, timezone

from django.utils import timezone as dj_tz

from apps.common.exceptions import APIError
from apps.common.permissions import request_organization_id


def parse_pagination(request, default_limit=50, max_limit=100):
    try:
        page = int(request.query_params.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        limit = int(request.query_params.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    if page < 1:
        page = 1
    if limit < 1 or limit > max_limit:
        limit = default_limit
    return page, limit, (page - 1) * limit


def list_payload(key, items, total, page, limit, **extra):
    data = {key: items, "total": total, "page": page, "limit": limit}
    data.update(extra)
    return data


def iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def user_iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_super(request) -> bool:
    user = request.user
    return bool(getattr(user, "is_super_admin_claim", False) or getattr(user, "is_super_admin", False))


def require_perm(request, resource: str, action: str):
    if is_super(request):
        return
    if not request.user.has_permission(resource, action):
        raise APIError("Forbidden", status_code=403)


def org_id(request):
    value = request_organization_id(request)
    if not value:
        raise APIError("Unauthorized", status_code=401)
    return value


def service_window_open(last_inbound_at) -> bool:
    if last_inbound_at is None:
        return False
    if last_inbound_at.tzinfo is None:
        last_inbound_at = last_inbound_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_inbound_at < timedelta(hours=24)


def normalize_phone(phone: str) -> str:
    return (phone or "").strip().lstrip("+")


def request_body(request) -> dict:
    return request.data if isinstance(request.data, dict) else {}


def soft_delete(obj):
    obj.deleted_at = dj_tz.now()
    obj.save(update_fields=["deleted_at", "updated_at"])


def uid(value) -> str:
    return str(value) if value else ""


def parse_date_range(from_str: str, to_str: str):
    """Parse YYYY-MM-DD dates; end is inclusive through end of day UTC."""
    from datetime import datetime, time as dtime

    try:
        start = datetime.strptime(from_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None, None, "Invalid start date format. Use YYYY-MM-DD"
    try:
        end_day = datetime.strptime(to_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None, None, "Invalid end date format. Use YYYY-MM-DD"
    end = datetime.combine(end_day.date(), dtime(23, 59, 59, 999999), tzinfo=timezone.utc)
    return start, end, ""


def parse_optional_date_range(start_str: str = "", end_str: str = ""):
    """Parse optional inclusive YYYY-MM-DD bounds as UTC datetimes."""
    from datetime import datetime, time as dtime

    start = end = None
    if start_str:
        try:
            start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None, None, "Invalid start_date. Use YYYY-MM-DD"
    if end_str:
        try:
            end_day = datetime.strptime(end_str, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None, None, "Invalid end_date. Use YYYY-MM-DD"
        end = datetime.combine(end_day, dtime(23, 59, 59, 999999), tzinfo=timezone.utc)
    if start and end and end < start:
        return None, None, "end_date must be on or after start_date"
    return start, end, ""


def period_bounds(request):
    """from/to query params, or current month through now."""
    now = datetime.now(timezone.utc)
    from_str = request.query_params.get("from") or ""
    to_str = request.query_params.get("to") or ""
    if from_str and to_str:
        start, end, err = parse_date_range(from_str, to_str)
        if err:
            return None, None, err
        return start, end, ""
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return start, now, ""


def previous_period(start, end):
    duration = end - start
    prev_end = start - timedelta(microseconds=1)
    prev_start = prev_end - duration
    return prev_start, prev_end


def pct_change(previous, current) -> float:
    previous = float(previous or 0)
    current = float(current or 0)
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return (current - previous) / previous * 100.0


def mask_phone(phone: str) -> str:
    phone = phone or ""
    if len(phone) <= 4:
        return phone
    return ("*" * (len(phone) - 4)) + phone[-4:]


def looks_like_phone(value: str) -> bool:
    value = value or ""
    if len(value) < 7:
        return False
    digits = sum(1 for c in value if c.isdigit())
    return digits >= 7 and digits / len(value) > 0.7


def mask_if_phone(value: str) -> str:
    return mask_phone(value) if looks_like_phone(value) else value


def org_masks_phones(oid) -> bool:
    from apps.accounts.models import Organization

    org = Organization.objects.filter(id=oid).first()
    settings = (org.settings or {}) if org else {}
    return bool(settings.get("mask_phone_numbers"))
