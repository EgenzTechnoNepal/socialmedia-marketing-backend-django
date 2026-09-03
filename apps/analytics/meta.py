import json
from datetime import datetime, timedelta, timezone

from rest_framework.decorators import api_view, permission_classes

from apps.common.envelope import error, success
from apps.common.http import org_id, parse_date_range, require_perm
from apps.common.permissions import CookieAuthenticated
from apps.common.tokens import redis_client
from apps.messaging.models import Template
from apps.whatsapp.models import WhatsAppAccount
from services import whatsapp_client
from services.whatsapp_client import WhatsAppError

CACHE_PREFIX = "meta:analytics:"
VALID_TYPES = {"analytics", "pricing_analytics", "template_analytics", "call_analytics"}
VALID_GRAN = {"HALF_HOUR", "DAY", "DAILY", "MONTH", "MONTHLY"}
TTL = {"HALF_HOUR": 3600, "DAY": 3 * 3600, "MONTH": 6 * 3600}


def _cache_key(oid, account_id, analytics_type, start, end, granularity):
    return f"{CACHE_PREFIX}{oid}:{account_id or 'all'}:{analytics_type}:{start}:{end}:{granularity}"


def _ttl(granularity: str) -> int:
    return TTL.get(granularity, TTL["DAY"])


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def meta_analytics(request):
    oid = org_id(request)
    require_perm(request, "analytics", "read")
    account_id = request.query_params.get("account_id") or ""
    analytics_type = request.query_params.get("analytics_type") or ""
    start_str = request.query_params.get("start") or ""
    end_str = request.query_params.get("end") or ""
    granularity = request.query_params.get("granularity") or "DAY"
    if not analytics_type:
        return error("analytics_type is required", http_status=400)
    if analytics_type not in VALID_TYPES:
        return error(
            "Invalid analytics_type. Must be one of: analytics, pricing_analytics, template_analytics, call_analytics",
            http_status=400,
        )
    if not start_str or not end_str:
        return error("start and end dates are required (YYYY-MM-DD format)", http_status=400)
    start_date, end_date, err = parse_date_range(start_str, end_str)
    if err:
        return error(err, http_status=400)
    if end_date < start_date:
        return error("End date must be after start date", http_status=400)
    if granularity not in VALID_GRAN:
        return error("Invalid granularity. Must be one of: HALF_HOUR, DAY, MONTH", http_status=400)
    days = int((end_date - start_date).total_seconds() / 86400)
    original = granularity
    if granularity in {"MONTH", "MONTHLY"} and days < 30:
        granularity = "DAY"
    if granularity == "HALF_HOUR" and days > 7:
        granularity = "DAY"
    if analytics_type == "template_analytics":
        if start_date < datetime.now(timezone.utc) - timedelta(days=90):
            return error("Template analytics have a 90-day lookback limit", http_status=400)
    start_unix = int(start_date.timestamp())
    end_unix = int(end_date.timestamp())
    if account_id:
        accounts = list(WhatsAppAccount.objects.filter(id=account_id, organization_id=oid))
        if not accounts:
            return error("Account not found", http_status=404)
    else:
        accounts = list(WhatsAppAccount.objects.filter(organization_id=oid))
    if not accounts:
        return success({"accounts": [], "message": "No WhatsApp accounts found"})
    key = _cache_key(oid, account_id, analytics_type, start_unix, end_unix, granularity)
    try:
        cached = redis_client().get(key)
        if cached:
            return success({"accounts": json.loads(cached), "cached": True})
    except Exception:
        pass
    results = []
    for account in accounts:
        account.decrypt_secrets()
        template_ids = []
        template_names = {}
        if analytics_type == "template_analytics":
            raw_ids = request.query_params.get("template_ids") or ""
            if raw_ids:
                try:
                    template_ids = json.loads(raw_ids)
                except json.JSONDecodeError:
                    template_ids = []
            if not template_ids:
                template_ids = list(
                    Template.objects.filter(
                        organization_id=oid,
                        whatsapp_account=account.name,
                    )
                    .exclude(meta_template_id="")
                    .values_list("meta_template_id", flat=True)
                )
            if not template_ids:
                results.append({"account_id": str(account.id), "account_name": account.name, "data": None})
                continue
        try:
            data = whatsapp_client.get_analytics(
                account, analytics_type, start_unix, end_unix, granularity, template_ids
            )
        except WhatsAppError:
            results.append({"account_id": str(account.id), "account_name": account.name, "data": None})
            continue
        if analytics_type == "template_analytics" and data:
            ids = {
                dp.get("template_id")
                for dp in ((data.get("template_analytics") or {}).get("data_points") or [])
                if dp.get("template_id")
            }
            if ids:
                for tpl in Template.objects.filter(organization_id=oid, meta_template_id__in=ids):
                    template_names[tpl.meta_template_id] = tpl.display_name or tpl.name
        item = {"account_id": str(account.id), "account_name": account.name, "data": data}
        if template_names:
            item["template_names"] = template_names
        results.append(item)
    try:
        redis_client().setex(key, _ttl(granularity), json.dumps(results))
    except Exception:
        pass
    payload = {"accounts": results, "cached": False}
    if granularity != original:
        payload["adjusted_granularity"] = granularity
        payload["original_granularity"] = original
    return success(payload)


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def meta_accounts(request):
    oid = org_id(request)
    require_perm(request, "analytics", "read")
    accounts = [
        {"id": str(a.id), "name": a.name, "phone_id": a.phone_id}
        for a in WhatsAppAccount.objects.filter(organization_id=oid).only("id", "name", "phone_id")
    ]
    return success({"accounts": accounts})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def refresh_meta_cache(request):
    oid = org_id(request)
    require_perm(request, "analytics", "write")
    pattern = f"{CACHE_PREFIX}{oid}:*"
    try:
        client = redis_client()
        for key in client.scan_iter(pattern):
            client.delete(key)
    except Exception:
        pass
    return success({"message": "Analytics cache cleared successfully"})
