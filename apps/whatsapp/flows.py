from copy import deepcopy

from rest_framework.decorators import api_view, permission_classes

from apps.chatbot.models import WhatsAppFlow
from apps.common.envelope import error, success
from apps.common.exceptions import APIError
from apps.common.http import iso, list_payload, org_id, parse_pagination, request_body, require_perm, soft_delete
from apps.common.permissions import CookieAuthenticated
from apps.whatsapp.models import resolve_account
from services import whatsapp_client
from services.whatsapp_client import WhatsAppError

COMPONENTS_WITHOUT_ID = {
    "TextHeading",
    "TextSubheading",
    "TextBody",
    "TextInput",
    "TextArea",
    "Dropdown",
    "RadioButtonsGroup",
    "CheckboxGroup",
    "DatePicker",
    "Image",
    "Footer",
}


def flow_payload(row: WhatsAppFlow) -> dict:
    return {
        "id": str(row.id),
        "whatsapp_account": row.whatsapp_account or "",
        "meta_flow_id": row.meta_flow_id or "",
        "name": row.name,
        "status": row.status or "DRAFT",
        "category": row.category or "",
        "json_version": row.json_version or "6.0",
        "flow_json": row.flow_json or {},
        "screens": row.screens or [],
        "preview_url": row.preview_url or "",
        "has_local_changes": bool(row.has_local_changes),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


def _get_flow(oid, flow_id) -> WhatsAppFlow:
    flow = WhatsAppFlow.objects.filter(id=flow_id, organization_id=oid).first()
    if not flow:
        raise APIError("Flow not found", status_code=404)
    return flow


def _perm(request, action: str):
    require_perm(request, "flows.whatsapp", action)


def sanitize_id(value: str) -> str:
    if all((c.isalpha() or c == "_") for c in value):
        return value
    out = []
    for c in value:
        if c.isdigit():
            out.append(chr(ord("A") + int(c)))
        elif c.isalpha() or c == "_":
            out.append(c)
    return "".join(out)


def _has_complete_action(children: list) -> bool:
    for child in children:
        if not isinstance(child, dict):
            continue
        action = child.get("on-click-action") or {}
        if isinstance(action, dict) and action.get("name") == "complete":
            return True
    return False


def validate_flow_structure(screens: list):
    if not screens:
        raise APIError("flow must have at least one screen", status_code=400)
    complete_indexes = []
    for i, screen in enumerate(screens):
        if not isinstance(screen, dict):
            continue
        layout = screen.get("layout") or {}
        children = layout.get("children") or []
        if isinstance(children, list) and _has_complete_action(children):
            complete_indexes.append(i)
    if not complete_indexes:
        raise APIError(
            "flow must have a Footer button with 'Complete Flow' action: add a Footer component to your last screen and set its action to 'Complete Flow'",
            status_code=400,
        )
    if len(screens) > 1:
        last = len(screens) - 1
        for idx in complete_indexes:
            if idx != last:
                raise APIError(
                    f"'Complete Flow' action should only be on the last screen. Screen {idx + 1} has a complete action but it's not the last screen. Use 'Navigate to Screen' action for intermediate screens",
                    status_code=400,
                )


def _form_fields(children: list) -> list[str]:
    names = []
    for child in children:
        if isinstance(child, dict) and child.get("name"):
            names.append(sanitize_id(str(child["name"])))
    return names


def collect_form_field_names(screens: list) -> list[str]:
    names = []
    for screen in screens:
        if not isinstance(screen, dict):
            continue
        layout = screen.get("layout") or {}
        children = layout.get("children") or []
        if isinstance(children, list):
            names.extend(_form_fields(children))
    return names


def collect_form_fields_per_screen(screens: list) -> dict[int, list[str]]:
    result = {}
    for i, screen in enumerate(screens):
        if not isinstance(screen, dict):
            continue
        layout = screen.get("layout") or {}
        children = layout.get("children") or []
        if isinstance(children, list):
            fields = _form_fields(children)
            if fields:
                result[i] = fields
    return result


def sanitize_components(children: list, all_field_names: list[str], previous_fields: list[str]) -> list:
    this_fields = _form_fields(children)
    this_set = set(this_fields)
    result = []
    for child in children:
        if not isinstance(child, dict):
            result.append(child)
            continue
        new_comp = deepcopy(child)
        if new_comp.get("type") in COMPONENTS_WITHOUT_ID:
            new_comp.pop("id", None)
        if new_comp.get("name"):
            new_comp["name"] = sanitize_id(str(new_comp["name"]))
        data_source = new_comp.get("data-source")
        if isinstance(data_source, list):
            cleaned = []
            for opt in data_source:
                if isinstance(opt, dict):
                    item = dict(opt)
                    if item.get("id"):
                        item["id"] = sanitize_id(str(item["id"]))
                    cleaned.append(item)
                else:
                    cleaned.append(opt)
            new_comp["data-source"] = cleaned
        action = new_comp.get("on-click-action")
        if isinstance(action, dict):
            new_action = dict(action)
            name = new_action.get("name")
            if name == "complete":
                payload = {}
                for field in all_field_names:
                    payload[field] = f"${{form.{field}}}" if field in this_set else f"${{data.{field}}}"
                new_action["payload"] = payload
            elif name == "navigate" and this_fields:
                payload = {field: f"${{data.{field}}}" for field in previous_fields}
                payload.update({field: f"${{form.{field}}}" for field in this_fields})
                new_action["payload"] = payload
            new_comp["on-click-action"] = new_action
        result.append(new_comp)
    return result


def sanitize_screens_for_meta(screens: list) -> list:
    screen_fields = collect_form_fields_per_screen(screens)
    all_fields = collect_form_field_names(screens)
    previous = []
    result = []
    for i, screen in enumerate(screens):
        if not isinstance(screen, dict):
            result.append(screen)
            continue
        new_screen = deepcopy(screen)
        if new_screen.get("id"):
            new_screen["id"] = sanitize_id(str(new_screen["id"]))
        if i > 0 and previous:
            data_model = dict(new_screen.get("data") or {}) if isinstance(new_screen.get("data"), dict) else {}
            for field in previous:
                data_model[field] = {"type": "string", "__example__": ""}
            new_screen["data"] = data_model
        layout = new_screen.get("layout")
        is_terminal = False
        if isinstance(layout, dict):
            new_layout = dict(layout)
            children = layout.get("children") or []
            if isinstance(children, list):
                sanitized = sanitize_components(children, all_fields, previous)
                new_layout["children"] = sanitized
                is_terminal = _has_complete_action(sanitized)
            new_screen["layout"] = new_layout
        if is_terminal:
            new_screen["terminal"] = True
        result.append(new_screen)
        previous.extend(screen_fields.get(i) or [])
    return result


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def flows_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        _perm(request, "read")
        page, limit, offset = parse_pagination(request)
        qs = WhatsAppFlow.objects.filter(organization_id=oid)
        account = request.query_params.get("account") or ""
        status = request.query_params.get("status") or ""
        search = request.query_params.get("search") or ""
        if account:
            qs = qs.filter(whatsapp_account=account)
        if status:
            qs = qs.filter(status=status)
        if search:
            qs = qs.filter(name__icontains=search)
        total = qs.count()
        rows = qs.order_by("-created_at")[offset : offset + limit]
        return success(list_payload("flows", [flow_payload(r) for r in rows], total, page, limit))

    _perm(request, "write")
    data = request_body(request)
    name = (data.get("name") or "").strip()
    account_name = (data.get("whatsapp_account") or "").strip()
    if not name:
        return error("Name is required", http_status=400)
    if not account_name:
        return error("WhatsApp account is required", http_status=400)
    try:
        resolve_account(oid, account_name)
    except APIError:
        return error("WhatsApp account not found", http_status=400)
    flow = WhatsAppFlow.objects.create(
        organization_id=oid,
        whatsapp_account=account_name,
        name=name,
        status="DRAFT",
        category=data.get("category") or "",
        json_version=data.get("json_version") or "6.0",
        flow_json=data.get("flow_json") or {},
        screens=data.get("screens") or [],
        has_local_changes=True,
    )
    return success({"flow": flow_payload(flow)})


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def flow_detail(request, flow_id):
    oid = org_id(request)
    flow = _get_flow(oid, flow_id)
    if request.method == "GET":
        _perm(request, "read")
        return success({"flow": flow_payload(flow)})
    if request.method == "DELETE":
        _perm(request, "delete")
        soft_delete(flow)
        return success({"message": "Flow deleted successfully"})
    _perm(request, "write")
    data = request_body(request)
    if data.get("name"):
        flow.name = data["name"]
    if data.get("category"):
        flow.category = data["category"]
    if data.get("json_version"):
        flow.json_version = data["json_version"]
    if data.get("flow_json") is not None:
        flow.flow_json = data["flow_json"]
    if data.get("screens") is not None:
        flow.screens = data["screens"]
    flow.has_local_changes = True
    flow.save()
    return success({"flow": flow_payload(flow)})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def save_flow_to_meta(request, flow_id):
    oid = org_id(request)
    _perm(request, "write")
    flow = _get_flow(oid, flow_id)
    if flow.status == "DEPRECATED":
        return error("Deprecated flows cannot be updated", http_status=400)
    try:
        account = resolve_account(oid, flow.whatsapp_account)
    except APIError:
        return error("WhatsApp account not found", http_status=400)
    try:
        meta_id = flow.meta_flow_id or ""
        if not meta_id:
            categories = [flow.category] if flow.category else []
            meta_id = whatsapp_client.create_flow(account, flow.name, categories)
        screens = flow.screens or []
        if screens:
            validate_flow_structure(screens)
            sanitized = sanitize_screens_for_meta(screens)
            whatsapp_client.update_flow_json(
                account,
                meta_id,
                {"version": flow.json_version or "6.0", "screens": sanitized},
            )
        flow.meta_flow_id = meta_id
        flow.status = "DRAFT"
        flow.has_local_changes = False
        flow.save(update_fields=["meta_flow_id", "status", "has_local_changes", "updated_at"])
    except APIError as exc:
        return error(str(exc), http_status=exc.status_code)
    except WhatsAppError:
        if not flow.meta_flow_id:
            flow.meta_flow_id = meta_id
            flow.save(update_fields=["meta_flow_id", "updated_at"])
        return error("Failed to update flow JSON", http_status=500)
    return success({"flow": flow_payload(flow), "message": "Flow saved to Meta successfully"})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def publish_flow(request, flow_id):
    oid = org_id(request)
    _perm(request, "write")
    flow = _get_flow(oid, flow_id)
    if flow.status != "DRAFT":
        return error("Only DRAFT flows can be published", http_status=400)
    if not flow.meta_flow_id:
        return error("Flow must be saved to Meta first before publishing", http_status=400)
    try:
        account = resolve_account(oid, flow.whatsapp_account)
    except APIError:
        return error("WhatsApp account not found", http_status=400)
    try:
        whatsapp_client.publish_flow(account, flow.meta_flow_id)
        preview = ""
        try:
            meta = whatsapp_client.get_flow(account, flow.meta_flow_id)
            preview = (meta or {}).get("preview_url") or ""
        except WhatsAppError:
            pass
        flow.status = "PUBLISHED"
        flow.preview_url = preview
        flow.save(update_fields=["status", "preview_url", "updated_at"])
    except WhatsAppError:
        return error("Failed to publish flow", http_status=500)
    return success({"flow": flow_payload(flow), "message": "Flow published successfully"})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def deprecate_flow(request, flow_id):
    oid = org_id(request)
    _perm(request, "write")
    flow = _get_flow(oid, flow_id)
    if flow.status != "PUBLISHED":
        return error("Only PUBLISHED flows can be deprecated", http_status=400)
    if flow.meta_flow_id:
        try:
            account = resolve_account(oid, flow.whatsapp_account)
        except APIError:
            return error("WhatsApp account not found", http_status=400)
        try:
            whatsapp_client.deprecate_flow(account, flow.meta_flow_id)
        except WhatsAppError:
            return error("Failed to deprecate flow in Meta", http_status=500)
    flow.status = "DEPRECATED"
    flow.save(update_fields=["status", "updated_at"])
    return success({"flow": flow_payload(flow), "message": "Flow deprecated successfully"})


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def duplicate_flow(request, flow_id):
    oid = org_id(request)
    _perm(request, "write")
    flow = _get_flow(oid, flow_id)
    copy = WhatsAppFlow.objects.create(
        organization_id=oid,
        whatsapp_account=flow.whatsapp_account,
        name=f"{flow.name} (Copy)",
        status="DRAFT",
        category=flow.category,
        json_version=flow.json_version,
        flow_json=flow.flow_json,
        screens=flow.screens,
        has_local_changes=True,
    )
    return success(
        {
            "flow": flow_payload(copy),
            "message": "Flow duplicated successfully. You can now edit and publish the new flow.",
        }
    )


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def sync_flows(request):
    oid = org_id(request)
    _perm(request, "write")
    account_name = (request_body(request).get("whatsapp_account") or "").strip()
    if not account_name:
        return error("WhatsApp account is required", http_status=400)
    try:
        account = resolve_account(oid, account_name)
    except APIError:
        return error("WhatsApp account not found", http_status=400)
    try:
        meta_flows = whatsapp_client.list_flows(account)
    except WhatsAppError:
        return error("Failed to fetch flows from Meta", http_status=500)
    synced = created = updated = 0
    for mf in meta_flows:
        meta_id = mf.get("id") or ""
        if not meta_id:
            continue
        category = ""
        cats = mf.get("categories") or []
        if cats:
            category = cats[0]
        flow_json = {}
        screens = []
        json_version = ""
        try:
            assets = whatsapp_client.get_flow_assets(account, meta_id)
        except WhatsAppError:
            assets = None
        if assets:
            flow_json = assets
            screens = assets.get("screens") or []
            json_version = assets.get("version") or ""
        existing = WhatsAppFlow.objects.filter(organization_id=oid, meta_flow_id=meta_id).first()
        if existing is None:
            WhatsAppFlow.objects.create(
                organization_id=oid,
                whatsapp_account=account_name,
                meta_flow_id=meta_id,
                name=mf.get("name") or "",
                status=mf.get("status") or "DRAFT",
                category=category,
                preview_url=mf.get("preview_url") or "",
                flow_json=flow_json,
                screens=screens,
                json_version=json_version or "6.0",
                has_local_changes=False,
            )
            created += 1
        else:
            existing.name = mf.get("name") or existing.name
            existing.status = mf.get("status") or existing.status
            existing.category = category
            existing.preview_url = mf.get("preview_url") or ""
            if assets:
                existing.flow_json = flow_json
                existing.screens = screens
                existing.json_version = json_version or existing.json_version
            existing.save()
            updated += 1
        synced += 1
    return success({"message": "Flows synced successfully", "synced": synced, "created": created, "updated": updated})
