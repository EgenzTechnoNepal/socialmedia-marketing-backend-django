import csv
import io
import json
from datetime import datetime, timezone

from django.db import connection
from django.http import HttpResponse
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser

from apps.common.envelope import error, success
from apps.common.http import mask_if_phone, mask_phone, org_id, org_masks_phones, request_body, require_perm
from apps.common.permissions import CookieAuthenticated
from apps.contacts.models import Contact

EXPORT_CONFIGS = {
    "contacts": {
        "resource": "contacts",
        "allowed": [
            "phone_number",
            "profile_name",
            "whats_app_account",
            "tags",
            "assigned_user_id",
            "last_message_at",
            "created_at",
            "updated_at",
        ],
        "default": ["phone_number", "profile_name", "tags"],
        "labels": {
            "phone_number": "Phone Number",
            "profile_name": "Name",
            "whats_app_account": "WhatsApp Account",
            "tags": "Tags",
            "assigned_user_id": "Assigned User ID",
            "last_message_at": "Last Message At",
            "created_at": "Created At",
            "updated_at": "Updated At",
        },
    },
    "tags": {
        "resource": "tags",
        "allowed": ["name", "color", "description", "created_at"],
        "default": ["name", "color", "description"],
        "labels": {"name": "Name", "color": "Color", "description": "Description", "created_at": "Created At"},
    },
}

IMPORT_CONFIGS = {
    "contacts": {
        "resource": "contacts",
        "required": ["phone_number"],
        "optional": ["profile_name", "whats_app_account", "tags", "assigned_user_id"],
        "unique": "phone_number",
    },
    "tags": {
        "resource": "tags",
        "required": ["name"],
        "optional": ["color", "description"],
        "unique": "name",
    },
}


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


def _escape_csv(cell: str) -> str:
    if cell and cell[0] in {"=", "@"}:
        return "'" + cell
    return cell


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def export_config(request, table):
    oid = org_id(request)
    config = EXPORT_CONFIGS.get(table)
    if not config:
        return error("Invalid table", http_status=400)
    require_perm(request, config["resource"], "export")
    columns = [{"key": col, "label": config["labels"].get(col, col)} for col in config["allowed"]]
    return success({"table": table, "columns": columns, "default_columns": config["default"]})


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def import_config(request, table):
    oid = org_id(request)
    config = IMPORT_CONFIGS.get(table)
    if not config:
        return error("Invalid table", http_status=400)
    require_perm(request, config["resource"], "import")
    labels = EXPORT_CONFIGS.get(table, {}).get("labels") or {}

    def cols(keys):
        return [{"key": k, "label": labels.get(k, k)} for k in keys]

    return success(
        {
            "table": table,
            "required_columns": cols(config["required"]),
            "optional_columns": cols(config["optional"]),
            "unique_column": config["unique"],
        }
    )


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def export_data(request):
    oid = org_id(request)
    data = request_body(request)
    table = data.get("table") or ""
    config = EXPORT_CONFIGS.get(table)
    if not config:
        return error("Invalid table", http_status=400)
    require_perm(request, config["resource"], "export")
    columns = data.get("columns") or config["default"]
    allowed = set(config["allowed"])
    for col in columns:
        if col not in allowed:
            return error(f"Column '{col}' is not allowed for export", http_status=400)
    safe = [col for col in config["allowed"] if col in set(columns)]
    filters = data.get("filters") or {}
    search = (filters.get("search") or "").strip()
    tags_filter = (filters.get("tags") or "").strip()
    rows_out = []
    if table == "contacts":
        qs = Contact.objects.filter(organization_id=oid)
        if search:
            from django.db.models import Q

            qs = qs.filter(Q(phone_number__icontains=search) | Q(profile_name__icontains=search))
        if tags_filter:
            from django.db.models import Q

            tag_q = Q()
            for tag in [t.strip() for t in tags_filter.split(",") if t.strip()]:
                tag_q |= Q(tags__contains=[tag])
            qs = qs.filter(tag_q)
        mask = org_masks_phones(oid)
        for contact in qs:
            mapping = {
                "phone_number": contact.phone_number or "",
                "profile_name": contact.profile_name or "",
                "whats_app_account": contact.whatsapp_account or "",
                "tags": ",".join(contact.tags or []) if isinstance(contact.tags, list) else "",
                "assigned_user_id": str(contact.assigned_user_id) if contact.assigned_user_id else "",
                "last_message_at": _fmt(contact.last_message_at),
                "created_at": _fmt(contact.created_at),
                "updated_at": _fmt(contact.updated_at),
            }
            if mask:
                mapping["phone_number"] = mask_phone(mapping["phone_number"])
                mapping["profile_name"] = mask_if_phone(mapping["profile_name"])
            rows_out.append([mapping.get(col, "") for col in safe])
    else:
        sql = "SELECT name, color, created_at FROM tags WHERE organization_id = %s"
        params = [str(oid)]
        if search:
            sql += " AND name ILIKE %s"
            params.append(f"%{search}%")
        sql += " ORDER BY name"
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            for name, color, created_at in cursor.fetchall():
                mapping = {"name": name, "color": color or "", "description": "", "created_at": _fmt(created_at)}
                rows_out.append([mapping.get(col, "") for col in safe])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([config["labels"].get(col, col) for col in safe])
    for row in rows_out:
        writer.writerow([_escape_csv(str(cell)) for cell in row])
    filename = f"{table}_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    response = HttpResponse(buf.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = f"attachment; filename={filename}"
    return response


def _match_column(header: str, allowed: list[str], labels: dict) -> str | None:
    header = header.strip()
    for col in allowed:
        if header.lower() == col.lower() or header.lower() == col.replace("_", " ").lower():
            return col
        if header.lower() == (labels.get(col) or "").lower():
            return col
    return None


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
@permission_classes([CookieAuthenticated])
def import_data(request):
    oid = org_id(request)
    table = request.data.get("table") or ""
    config = IMPORT_CONFIGS.get(table)
    if not config:
        return error("Invalid table", http_status=400)
    require_perm(request, config["resource"], "import")
    update_on_dup = str(request.data.get("update_on_duplicate") or "").lower() == "true"
    mapping_raw = request.data.get("column_mapping") or ""
    column_mapping = {}
    if mapping_raw:
        try:
            column_mapping = json.loads(mapping_raw) if isinstance(mapping_raw, str) else mapping_raw
        except json.JSONDecodeError:
            column_mapping = {}
    upload = request.FILES.get("file")
    if not upload:
        return error("file is required", http_status=400)
    raw = upload.read()
    if len(raw) > 10 * 1024 * 1024:
        return error("File too large", http_status=400)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return error("Failed to read CSV header", http_status=400)
    labels = EXPORT_CONFIGS.get(table, {}).get("labels") or {}
    allowed = config["required"] + config["optional"]
    index = {}
    for i, h in enumerate(header):
        mapped = column_mapping.get(h.strip()) if isinstance(column_mapping, dict) else None
        key = mapped or _match_column(h, allowed, labels)
        if key:
            index[key] = i
    for req in config["required"]:
        if req not in index:
            return error(f"Required column '{req}' not found in CSV", http_status=400)
    created = updated = skipped = errors = 0
    messages = []
    for row_num, record in enumerate(reader, start=2):
        if row_num > 10001:
            messages.append("Import limited to 10000 rows")
            break
        values = {}
        row_error = False
        for col, idx in index.items():
            if idx >= len(record):
                continue
            val = (record[idx] or "").strip()
            if len(val) > 10000:
                errors += 1
                messages.append(f"Row {row_num}: {col} exceeds max length")
                row_error = True
                break
            if col == "phone_number":
                phone = val.lstrip("+")
                if not phone:
                    messages.append(f"Row {row_num}: phone number is required")
                    row_error = True
                    break
                values[col] = phone
            elif col == "assigned_user_id":
                if val:
                    values[col] = val
            elif col == "tags":
                values[col] = [p.strip() for p in val.split(",") if p.strip()] if val else []
            elif val:
                values[col] = val
        if row_error:
            errors += 1
            continue
        missing = [c for c in config["required"] if c not in values]
        if missing:
            errors += 1
            messages.append(f"Row {row_num}: missing required field '{missing[0]}'")
            continue
        if table == "contacts":
            phone = values["phone_number"]
            existing = Contact.objects.filter(organization_id=oid, phone_number=phone).first()
            if existing:
                if update_on_dup:
                    if "profile_name" in values:
                        existing.profile_name = values["profile_name"]
                    if "whats_app_account" in values:
                        existing.whatsapp_account = values["whats_app_account"]
                    if "tags" in values:
                        existing.tags = values["tags"]
                    if "assigned_user_id" in values:
                        existing.assigned_user_id = values["assigned_user_id"]
                    existing.save()
                    updated += 1
                else:
                    skipped += 1
            else:
                Contact.objects.create(
                    organization_id=oid,
                    phone_number=phone,
                    profile_name=values.get("profile_name") or "",
                    whatsapp_account=values.get("whats_app_account") or "",
                    tags=values.get("tags") or [],
                    assigned_user_id=values.get("assigned_user_id") or None,
                )
                created += 1
        else:
            name = values["name"]
            color = values.get("color") or ""
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT name FROM tags WHERE organization_id = %s AND name = %s",
                    [str(oid), name],
                )
                exists = cursor.fetchone()
                if exists:
                    if update_on_dup:
                        cursor.execute(
                            "UPDATE tags SET color = %s, updated_at = NOW() WHERE organization_id = %s AND name = %s",
                            [color, str(oid), name],
                        )
                        updated += 1
                    else:
                        skipped += 1
                else:
                    cursor.execute(
                        """
                        INSERT INTO tags (organization_id, name, color, created_at, updated_at)
                        VALUES (%s, %s, %s, NOW(), NOW())
                        """,
                        [str(oid), name, color],
                    )
                    created += 1
    return success(
        {"created": created, "updated": updated, "skipped": skipped, "errors": errors, "messages": messages}
    )
