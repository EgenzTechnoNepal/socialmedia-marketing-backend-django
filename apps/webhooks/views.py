import ipaddress
import json
import logging
import os
import re
import secrets
import subprocess
import urllib.parse
import urllib.request

from django.http import HttpResponseRedirect
from django.utils.crypto import get_random_string
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny

from apps.accounts.models import Organization
from apps.common.envelope import error, success
from apps.common.http import iso, list_payload, org_id, parse_pagination, request_body, require_perm, soft_delete
from apps.common.permissions import CookieAuthenticated
from apps.common.tokens import redis_client
from apps.contacts.models import Contact
from apps.webhooks.dispatch import AVAILABLE_EVENTS, send_webhook_once
from apps.webhooks.models import CustomAction, Webhook
from services.templates import nested_get, process_template

logger = logging.getLogger(__name__)

REDIRECT_TTL = 30


def validate_webhook_url(raw: str) -> str:
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return "invalid URL"
    if parsed.scheme not in {"http", "https"}:
        return "URL scheme must be http or https"
    host = parsed.hostname or ""
    if not host:
        return "URL must have a hostname"
    lower = host.lower()
    if lower in {"localhost", "0.0.0.0"} or lower.endswith(".local") or lower.endswith(".internal"):
        return "URL must not point to internal addresses"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
            return "URL must not point to internal addresses"
    except ValueError:
        pass
    return ""


def webhook_payload(row: Webhook) -> dict:
    headers = {}
    if isinstance(row.headers, dict):
        headers = {k: v for k, v in row.headers.items() if isinstance(v, str)}
    return {
        "id": str(row.id),
        "name": row.name,
        "url": row.url,
        "events": row.events or [],
        "headers": headers,
        "is_active": row.is_active,
        "has_secret": bool(row.secret),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def webhooks_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        require_perm(request, "webhooks", "read")
        page, limit, offset = parse_pagination(request)
        qs = Webhook.objects.filter(organization_id=oid)
        search = request.query_params.get("search") or ""
        if search:
            qs = qs.filter(name__icontains=search)
        total = qs.count()
        rows = list(qs.order_by("-created_at")[offset : offset + limit])
        payload = list_payload("webhooks", [webhook_payload(w) for w in rows], total, page, limit)
        payload["available_events"] = AVAILABLE_EVENTS
        return success(payload)
    require_perm(request, "webhooks", "write")
    data = request_body(request)
    if not data.get("name") or not data.get("url"):
        return error("name and url are required", http_status=400)
    err = validate_webhook_url(data["url"])
    if err:
        return error(err, http_status=400)
    events = data.get("events") or []
    if not events:
        return error("at least one event must be selected", http_status=400)
    secret = data.get("secret") or secrets.token_hex(16)
    row = Webhook.objects.create(
        organization_id=oid,
        name=data["name"],
        url=data["url"],
        events=events,
        headers=data.get("headers") or {},
        secret=secret,
        is_active=True,
    )
    return success(webhook_payload(row), http_status=201)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def webhook_detail(request, webhook_id):
    oid = org_id(request)
    row = Webhook.objects.filter(id=webhook_id, organization_id=oid).first()
    if not row:
        return error("Webhook not found", http_status=404)
    if request.method == "GET":
        require_perm(request, "webhooks", "read")
        return success(webhook_payload(row))
    if request.method == "DELETE":
        require_perm(request, "webhooks", "delete")
        soft_delete(row)
        return success({"message": "Webhook deleted successfully"})
    require_perm(request, "webhooks", "write")
    data = request_body(request)
    if data.get("name") is not None:
        row.name = data["name"]
    if data.get("url") is not None:
        err = validate_webhook_url(data["url"])
        if err:
            return error(err, http_status=400)
        row.url = data["url"]
    if data.get("events") is not None:
        row.events = data["events"]
    if data.get("headers") is not None:
        row.headers = data["headers"]
    if data.get("secret"):
        row.secret = data["secret"]
    if data.get("is_active") is not None:
        row.is_active = data["is_active"]
    row.save()
    return success(webhook_payload(row))


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def test_webhook(request, webhook_id):
    oid = org_id(request)
    require_perm(request, "webhooks", "write")
    row = Webhook.objects.filter(id=webhook_id, organization_id=oid).first()
    if not row:
        return error("Webhook not found", http_status=404)
    try:
        send_webhook_once(row, "webhook.test", {"message": "This is a test webhook from Whatomate"})
    except Exception as exc:
        logger.warning("webhook test failed: %s", exc)
        return error("Webhook test failed", http_status=502)
    return success({"message": "Test webhook sent successfully"})


def action_payload(row: CustomAction) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "icon": row.icon or "",
        "action_type": row.action_type,
        "config": row.config or {},
        "is_active": row.is_active,
        "display_order": row.display_order or 0,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


def _validate_action(action_type: str, config: dict) -> str:
    config = config or {}
    if action_type == "webhook":
        if not config.get("url"):
            return "URL is required for webhook actions"
        return validate_webhook_url(config["url"])
    if action_type == "url":
        if not config.get("url"):
            return "URL is required for URL actions"
        return ""
    if action_type == "javascript":
        if "code" not in config:
            return "Code is required for JavaScript actions"
        return ""
    return "Invalid action type. Must be webhook, url, or javascript"


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def custom_actions_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        page, limit, offset = parse_pagination(request)
        qs = CustomAction.objects.filter(organization_id=oid)
        search = request.query_params.get("search") or ""
        if search:
            qs = qs.filter(name__icontains=search)
        total = qs.count()
        rows = list(qs.order_by("display_order", "-created_at")[offset : offset + limit])
        return success(list_payload("custom_actions", [action_payload(a) for a in rows], total, page, limit))
    data = request_body(request)
    if not data.get("name"):
        return error("Name is required", http_status=400)
    action_type = data.get("action_type") or ""
    err = _validate_action(action_type, data.get("config") or {})
    if err:
        return error(err, http_status=400)
    if CustomAction.objects.filter(organization_id=oid, name=data["name"]).exists():
        return error("A custom action with this name already exists", http_status=409)
    row = CustomAction.objects.create(
        organization_id=oid,
        name=data["name"],
        icon=data.get("icon") or "",
        action_type=action_type,
        config=data.get("config") or {},
        is_active=data.get("is_active", True),
        display_order=data.get("display_order") or 0,
    )
    return success(action_payload(row), http_status=201)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def custom_action_detail(request, action_id):
    oid = org_id(request)
    row = CustomAction.objects.filter(id=action_id, organization_id=oid).first()
    if not row:
        return error("Custom action not found", http_status=404)
    if request.method == "GET":
        return success(action_payload(row))
    if request.method == "DELETE":
        soft_delete(row)
        return success({"message": "Custom action deleted successfully"})
    data = request_body(request)
    action_type = data.get("action_type", row.action_type)
    config = data.get("config", row.config)
    err = _validate_action(action_type, config or {})
    if err:
        return error(err, http_status=400)
    if data.get("name") is not None:
        row.name = data["name"]
    if data.get("icon") is not None:
        row.icon = data["icon"]
    row.action_type = action_type
    row.config = config or {}
    if data.get("is_active") is not None:
        row.is_active = data["is_active"]
    if data.get("display_order") is not None:
        row.display_order = data["display_order"]
    row.save()
    return success(action_payload(row))


def _action_context(contact: Contact, user, org: Organization) -> dict:
    return {
        "contact": {
            "id": str(contact.id),
            "phone_number": contact.phone_number,
            "name": contact.profile_name or "",
            "profile_name": contact.profile_name or "",
            "tags": contact.tags or [],
            "metadata": contact.metadata or {},
        },
        "user": {"id": str(user.id), "name": user.full_name, "email": user.email},
        "organization": {"id": str(org.id), "name": org.name},
    }


def _replace_vars(template: str, context: dict) -> str:
    if not template:
        return ""

    def repl(match):
        path = match.group(1)
        value = nested_get(context, path)
        if value is None:
            # allow contact.phone_number style via dotted plus also {{phone_number}}
            parts = path.split(".")
            if len(parts) == 1:
                for group in context.values():
                    if isinstance(group, dict) and parts[0] in group:
                        return str(group[parts[0]])
            return ""
        return str(value)

    return re.sub(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}", repl, template)


def _store_redirect(url: str) -> str:
    token = secrets.token_hex(16)
    try:
        redis_client().setex(f"custom-action-redirect:{token}", REDIRECT_TTL, url)
    except Exception:
        logger.exception("failed to store redirect token")
    return f"/api/custom-actions/redirect/{token}"


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def execute_custom_action(request, action_id):
    oid = org_id(request)
    row = CustomAction.objects.filter(id=action_id, organization_id=oid).first()
    if not row:
        return error("Custom action not found", http_status=404)
    if not row.is_active:
        return error("Custom action is not active", http_status=400)
    data = request_body(request)
    contact = Contact.objects.filter(id=data.get("contact_id"), organization_id=oid).first()
    if not contact:
        return error("Contact not found", http_status=404)
    org = Organization.objects.get(id=oid)
    context = _action_context(contact, request.user, org)
    config = row.config or {}
    try:
        if row.action_type == "webhook":
            result = _exec_webhook_action(config, context)
        elif row.action_type == "url":
            result = {
                "success": True,
                "message": "Opening URL",
                "redirect_url": _store_redirect(_replace_vars(config.get("url") or "", context)),
            }
        elif row.action_type == "javascript":
            result = _exec_js_action(config, context)
        else:
            return error("Unknown action type", http_status=400)
    except Exception:
        logger.exception("custom action failed")
        return success({"success": False, "message": "Action execution failed", "toast": {"message": "Action failed", "type": "error"}})
    return success(result)


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def custom_action_redirect(request, token):
    try:
        url = redis_client().get(f"custom-action-redirect:{token}")
        redis_client().delete(f"custom-action-redirect:{token}")
    except Exception:
        url = None
    if not url:
        return error("Invalid or expired redirect token", http_status=404)
    return HttpResponseRedirect(url)


def _exec_webhook_action(config, context):
    url = _replace_vars(config.get("url") or "", context)
    method = (config.get("method") or "POST").upper()
    body = _replace_vars(config.get("body") or "", context)
    headers = {"Content-Type": "application/json"}
    extra = config.get("headers") or {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(value, str):
                headers[key] = _replace_vars(value, context)
    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = resp.read().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(payload) if payload else {}
    except Exception:
        parsed = {"raw": payload}
    return {
        "success": True,
        "message": "Webhook executed",
        "toast": {"message": "Action completed", "type": "success"},
        "data": parsed if isinstance(parsed, dict) else {"result": parsed},
    }


def _exec_js_action(config, context):
    code = config.get("code") or ""
    if not code:
        return {"success": True, "message": "No code to execute"}
    script = (
        "const context = "
        + json.dumps(context)
        + "; const contact = context.contact; const user = context.user; const organization = context.organization; "
        + f"const result = (function(context, contact, user, organization) {{ {code} }})(context, contact, user, organization); "
        + "process.stdout.write(JSON.stringify(result === undefined ? {} : result));"
    )
    try:
        completed = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "NODE_OPTIONS": "--no-experimental-fetch"},
        )
    except FileNotFoundError:
        return {"success": False, "message": "JavaScript runtime is not available", "toast": {"message": "Action failed", "type": "error"}}
    if completed.returncode != 0:
        return {"success": False, "message": completed.stderr.strip() or "javascript execution error", "toast": {"message": "Action failed", "type": "error"}}
    try:
        js_result = json.loads(completed.stdout or "{}")
    except Exception:
        js_result = {}
    result = {
        "success": True,
        "message": js_result.get("message") if isinstance(js_result, dict) else "JavaScript action executed",
        "toast": {"message": "Action completed", "type": "success"},
    }
    if isinstance(js_result, dict):
        if isinstance(js_result.get("toast"), dict):
            result["toast"] = {
                "message": str(js_result["toast"].get("message") or ""),
                "type": str(js_result["toast"].get("type") or "success"),
            }
        if js_result.get("clipboard"):
            result["clipboard"] = js_result["clipboard"]
        if js_result.get("url"):
            result["redirect_url"] = _store_redirect(str(js_result["url"]))
        if js_result.get("message"):
            result["message"] = js_result["message"]
    return result
