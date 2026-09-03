from django.db import connection
from django.db.models import Max, Q
from rest_framework.decorators import api_view, permission_classes

from apps.analytics.models import Widget
from apps.common.envelope import error, success
from apps.common.exceptions import APIError
from apps.common.http import iso, org_id, pct_change, period_bounds, previous_period, request_body, require_perm, soft_delete
from apps.common.permissions import CookieAuthenticated

DATA_SOURCES = {
    "messages": ["status", "direction", "message_type", "whatsapp_account"],
    "contacts": ["whatsapp_account", "is_read"],
    "campaigns": ["status", "message_status"],
    "transfers": ["status", "source"],
    "sessions": ["status"],
}
METRICS = ["count", "sum", "avg"]
DISPLAY_TYPES = ["number", "percentage", "chart", "table", "shortcuts"]
STATIC_TYPES = {"shortcuts"}
ALLOWED_FILTERS = {
    "messages": {
        "status",
        "direction",
        "message_type",
        "contact_id",
        "sent_by_user_id",
        "whatsapp_account",
        "conversation_id",
        "template_name",
    },
    "contacts": {"status", "assigned_user_id", "whatsapp_account"},
    "campaigns": {"status", "template_name", "created_by_id", "whatsapp_account"},
    "transfers": {"status", "team_id", "agent_id", "from_team", "to_team"},
    "sessions": {"status", "flow_id"},
}
COLUMN_MAP = {"whatsapp_account": "whats_app_account"}
TABLES = {
    "messages": ("messages", "created_at"),
    "contacts": ("contacts", "last_message_at"),
    "campaigns": ("bulk_message_campaigns", "created_at"),
    "transfers": ("agent_transfers", "transferred_at"),
    "sessions": ("chatbot_sessions", "created_at"),
}
GROUP_FIELDS = {
    "status",
    "message_status",
    "direction",
    "message_type",
    "assigned_user_id",
    "whatsapp_account",
    "is_read",
    "source",
    "channel",
    "is_active",
    "priority",
    "category",
    "type",
    "action_type",
    "provider",
}
TABLE_SQL = {
    "messages": (
        """SELECT m.id, COALESCE(c.profile_name, c.phone_number) as label,
            LEFT(m.content, 80) as sub_label, m.status, m.direction, m.created_at
            FROM messages m LEFT JOIN contacts c ON c.id = m.contact_id
            WHERE m.organization_id = %s AND m.created_at >= %s AND m.created_at <= %s""",
        " ORDER BY m.created_at DESC LIMIT 10",
    ),
    "contacts": (
        """SELECT id, COALESCE(profile_name, phone_number) as label,
            phone_number as sub_label, '' as status, '' as direction, last_message_at as created_at
            FROM contacts
            WHERE organization_id = %s AND last_message_at >= %s AND last_message_at <= %s""",
        " ORDER BY last_message_at DESC LIMIT 10",
    ),
    "campaigns": (
        """SELECT id, name as label, status as sub_label, status, '' as direction, created_at
            FROM bulk_message_campaigns
            WHERE organization_id = %s AND created_at >= %s AND created_at <= %s""",
        " ORDER BY created_at DESC LIMIT 10",
    ),
    "transfers": (
        """SELECT t.id, COALESCE(c.profile_name, c.phone_number) as label,
            t.source as sub_label, t.status, '' as direction, t.transferred_at as created_at
            FROM agent_transfers t LEFT JOIN contacts c ON c.id = t.contact_id
            WHERE t.organization_id = %s AND t.transferred_at >= %s AND t.transferred_at <= %s""",
        " ORDER BY t.transferred_at DESC LIMIT 10",
    ),
    "sessions": (
        """SELECT s.id, COALESCE(c.profile_name, c.phone_number) as label,
            s.status as sub_label, s.status, '' as direction, s.created_at
            FROM chatbot_sessions s LEFT JOIN contacts c ON c.id = s.contact_id
            WHERE s.organization_id = %s AND s.created_at >= %s AND s.created_at <= %s""",
        " ORDER BY s.created_at DESC LIMIT 10",
    ),
}


def _col(field: str) -> str:
    return COLUMN_MAP.get(field, field)


def _filters(raw) -> list[dict]:
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append(
                {
                    "field": item.get("field") or "",
                    "operator": item.get("operator") or "equals",
                    "value": str(item.get("value") if item.get("value") is not None else ""),
                }
            )
    return out


def widget_payload(widget: Widget, user_id) -> dict:
    return {
        "id": str(widget.id),
        "name": widget.name,
        "description": widget.description or "",
        "data_source": widget.data_source,
        "metric": widget.metric,
        "field": widget.field or "",
        "filters": _filters(widget.filters),
        "display_type": widget.display_type,
        "chart_type": widget.chart_type or "",
        "group_by_field": widget.group_by_field or "",
        "show_change": bool(widget.show_change),
        "color": widget.color or "",
        "size": widget.size or "small",
        "display_order": int(widget.display_order or 0),
        "grid_x": int(widget.grid_x or 0),
        "grid_y": int(widget.grid_y or 0),
        "grid_w": int(widget.grid_w or 0),
        "grid_h": int(widget.grid_h or 0),
        "config": widget.config or {},
        "is_shared": bool(widget.is_shared),
        "is_default": bool(widget.is_default),
        "is_owner": str(widget.user_id) == str(user_id) if widget.user_id else False,
        "created_at": iso(widget.created_at).replace("+00:00", "Z") if widget.created_at else "",
        "updated_at": iso(widget.updated_at).replace("+00:00", "Z") if widget.updated_at else "",
    }


def _visible(oid, user_id):
    return Widget.objects.filter(organization_id=oid).filter(Q(user_id=user_id) | Q(is_shared=True))


def _get_widget(oid, user_id, widget_id) -> Widget:
    widget = _visible(oid, user_id).filter(id=widget_id).first()
    if not widget:
        raise APIError("Widget not found", status_code=404)
    return widget


def _filter_sql(data_source: str, filters: list[dict]):
    clauses = []
    args = []
    allowed = ALLOWED_FILTERS.get(data_source) or set()
    for item in filters:
        field = item.get("field") or ""
        if field not in allowed:
            continue
        col = _col(field)
        op = item.get("operator") or "equals"
        value = item.get("value") or ""
        if op == "not_equals":
            clauses.append(f"{col} != %s")
            args.append(value)
        elif op == "contains":
            clauses.append(f"{col} ILIKE %s")
            args.append(f"%{value}%")
        elif op == "gt":
            clauses.append(f"{col} > %s")
            args.append(value)
        elif op == "lt":
            clauses.append(f"{col} < %s")
            args.append(value)
        elif op == "gte":
            clauses.append(f"{col} >= %s")
            args.append(value)
        elif op == "lte":
            clauses.append(f"{col} <= %s")
            args.append(value)
        else:
            clauses.append(f"{col} = %s")
            args.append(value)
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""
    return extra, args


def _scalar_count(data_source, oid, filters, start, end) -> float:
    table, date_field = TABLES[data_source]
    extra, args = _filter_sql(data_source, filters)
    sql = f"SELECT COUNT(*) FROM {table} WHERE organization_id = %s AND {date_field} >= %s AND {date_field} <= %s{extra}"
    with connection.cursor() as cursor:
        cursor.execute(sql, [str(oid), start, end, *args])
        return float(cursor.fetchone()[0] or 0)


def _transfer_avg(oid, filters, start, end) -> float:
    extra, args = _filter_sql("transfers", filters)
    sql = f"""
        SELECT COALESCE(AVG(EXTRACT(EPOCH FROM (resumed_at - transferred_at))/60), 0)
        FROM agent_transfers
        WHERE organization_id = %s AND transferred_at >= %s AND transferred_at <= %s
          AND status = 'resumed' AND resumed_at IS NOT NULL{extra}
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [str(oid), start, end, *args])
        return float(cursor.fetchone()[0] or 0)


def _chart_data(data_source, oid, filters, start, end):
    table, date_field = TABLES[data_source]
    extra, args = _filter_sql(data_source, filters)
    sql = f"""
        SELECT DATE_TRUNC('day', {date_field}) as date, COUNT(*) as count
        FROM {table}
        WHERE organization_id = %s AND {date_field} >= %s AND {date_field} <= %s{extra}
        GROUP BY DATE_TRUNC('day', {date_field}) ORDER BY date ASC
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [str(oid), start, end, *args])
        rows = cursor.fetchall()
    return [{"label": row[0].strftime("%b %d") if row[0] else "", "value": float(row[1] or 0)} for row in rows]


def _grouped_data(data_source, group_field, oid, filters, start, end):
    if data_source == "campaigns" and group_field == "message_status":
        extra, args = _filter_sql("campaigns", filters)
        sql = f"""
            SELECT COALESCE(SUM(sent_count), 0), COALESCE(SUM(delivered_count), 0),
                   COALESCE(SUM(read_count), 0), COALESCE(SUM(failed_count), 0)
            FROM bulk_message_campaigns
            WHERE organization_id = %s AND created_at >= %s AND created_at <= %s{extra}
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, [str(oid), start, end, *args])
            sent, delivered, read, failed = cursor.fetchone()
        return [
            {"label": "sent", "value": float(sent or 0)},
            {"label": "delivered", "value": float(delivered or 0)},
            {"label": "read", "value": float(read or 0)},
            {"label": "failed", "value": float(failed or 0)},
        ]
    table, date_field = TABLES[data_source]
    col = _col(group_field)
    extra, args = _filter_sql(data_source, filters)
    sql = f"""
        SELECT {col} as label, COUNT(*) as value
        FROM {table}
        WHERE organization_id = %s AND {date_field} >= %s AND {date_field} <= %s{extra}
        GROUP BY {col} ORDER BY value DESC
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [str(oid), start, end, *args])
        rows = cursor.fetchall()
    return [{"label": row[0] or "(empty)", "value": float(row[1] or 0)} for row in rows]


def _grouped_series(data_source, group_field, oid, filters, start, end):
    if data_source == "campaigns" and group_field == "message_status":
        extra, args = _filter_sql("campaigns", filters)
        sql = f"""
            SELECT DATE_TRUNC('day', created_at) as date,
                COALESCE(SUM(sent_count), 0), COALESCE(SUM(delivered_count), 0),
                COALESCE(SUM(read_count), 0), COALESCE(SUM(failed_count), 0)
            FROM bulk_message_campaigns
            WHERE organization_id = %s AND created_at >= %s AND created_at <= %s{extra}
            GROUP BY DATE_TRUNC('day', created_at) ORDER BY date ASC
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, [str(oid), start, end, *args])
            rows = cursor.fetchall()
        labels = [row[0].strftime("%b %d") if row[0] else "" for row in rows]
        return {
            "labels": labels,
            "datasets": [
                {"label": "sent", "data": [float(r[1] or 0) for r in rows]},
                {"label": "delivered", "data": [float(r[2] or 0) for r in rows]},
                {"label": "read", "data": [float(r[3] or 0) for r in rows]},
                {"label": "failed", "data": [float(r[4] or 0) for r in rows]},
            ],
        }
    table, date_field = TABLES[data_source]
    col = _col(group_field)
    extra, args = _filter_sql(data_source, filters)
    sql = f"""
        SELECT DATE_TRUNC('day', {date_field}) as date, {col} as group_value, COUNT(*) as count
        FROM {table}
        WHERE organization_id = %s AND {date_field} >= %s AND {date_field} <= %s{extra}
        GROUP BY DATE_TRUNC('day', {date_field}), {col} ORDER BY date ASC
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [str(oid), start, end, *args])
        rows = cursor.fetchall()
    labels = []
    groups = []
    seen_dates = set()
    seen_groups = set()
    lookup = {}
    for date, group, count in rows:
        label = date.strftime("%b %d") if date else ""
        gv = group or "(empty)"
        if label not in seen_dates:
            seen_dates.add(label)
            labels.append(label)
        if gv not in seen_groups:
            seen_groups.add(gv)
            groups.append(gv)
        lookup.setdefault(gv, {})[label] = float(count or 0)
    return {
        "labels": labels,
        "datasets": [{"label": g, "data": [lookup.get(g, {}).get(d, 0) for d in labels]} for g in groups],
    }


def _table_rows(data_source, oid, filters, start, end):
    spec = TABLE_SQL.get(data_source)
    if not spec:
        return []
    extra, args = _filter_sql(data_source, filters)
    sql = spec[0] + extra + spec[1]
    with connection.cursor() as cursor:
        cursor.execute(sql, [str(oid), start, end, *args])
        rows = cursor.fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "id": str(row[0]),
                "label": row[1] or "",
                "sub_label": row[2] or "",
                "status": row[3] or "",
                "direction": row[4] or "",
                "created_at": iso(row[5]) if row[5] else "",
            }
        )
    return out


def execute_widget(oid, widget: Widget, from_str="", to_str=""):
    class Req:
        query_params = {"from": from_str, "to": to_str}

    start, end, err = period_bounds(Req())
    if err:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        end = now
    prev_start, prev_end = previous_period(start, end)
    filters = _filters(widget.filters)
    data = {
        "widget_id": str(widget.id),
        "value": 0.0,
        "change": 0.0,
        "chart_data": [],
        "prev_value": 0.0,
        "data_points": [],
        "grouped_series": None,
        "table_rows": [],
    }
    if widget.display_type in STATIC_TYPES:
        return data
    if widget.display_type == "table":
        if widget.group_by_field and widget.group_by_field in GROUP_FIELDS:
            data["data_points"] = _grouped_data(widget.data_source, widget.group_by_field, oid, filters, start, end)
        else:
            data["table_rows"] = _table_rows(widget.data_source, oid, filters, start, end)
        return data
    if widget.data_source not in TABLES:
        return data
    if widget.data_source == "transfers" and widget.metric == "avg" and widget.field == "resolution_time":
        current = _transfer_avg(oid, filters, start, end)
        previous = _transfer_avg(oid, filters, prev_start, prev_end)
    else:
        current = _scalar_count(widget.data_source, oid, filters, start, end)
        previous = _scalar_count(widget.data_source, oid, filters, prev_start, prev_end)
    data["value"] = current
    data["prev_value"] = previous
    data["change"] = pct_change(previous, current)
    if widget.display_type == "chart":
        group = widget.group_by_field or ""
        if group and group in GROUP_FIELDS:
            if widget.chart_type == "line":
                data["grouped_series"] = _grouped_series(widget.data_source, group, oid, filters, start, end)
            else:
                data["data_points"] = _grouped_data(widget.data_source, group, oid, filters, start, end)
        else:
            data["chart_data"] = _chart_data(widget.data_source, oid, filters, start, end)
    return data


def _apply_widget_fields(widget: Widget, data: dict, create=False):
    display_type = data.get("display_type") or (widget.display_type if not create else "number")
    if display_type not in DISPLAY_TYPES:
        raise APIError("Invalid display type", status_code=400)
    if display_type in STATIC_TYPES:
        data_source = display_type
        metric = "count"
    else:
        data_source = data.get("data_source") or widget.data_source
        metric = data.get("metric") or widget.metric
        if not data_source:
            raise APIError("Data source is required", status_code=400)
        if not metric:
            raise APIError("Metric is required", status_code=400)
        if data_source not in DATA_SOURCES:
            raise APIError("Invalid data source", status_code=400)
        if metric not in METRICS:
            raise APIError("Invalid metric", status_code=400)
    group_by = data.get("group_by_field") if "group_by_field" in data else widget.group_by_field
    if group_by and display_type not in STATIC_TYPES:
        fields = DATA_SOURCES.get(data_source) or []
        if group_by not in fields and group_by not in GROUP_FIELDS:
            raise APIError("Invalid group by field for this data source", status_code=400)
    widget.name = data.get("name") or widget.name
    if "description" in data:
        widget.description = data.get("description") or ""
    widget.data_source = data_source
    widget.metric = metric
    if "field" in data or create:
        widget.field = data.get("field") or ""
    if "filters" in data or create:
        widget.filters = data.get("filters") or []
    widget.display_type = display_type
    if "chart_type" in data or create:
        widget.chart_type = data.get("chart_type") or ""
    if "group_by_field" in data or create:
        widget.group_by_field = group_by or ""
    if data.get("show_change") is not None:
        widget.show_change = bool(data["show_change"])
    elif create:
        widget.show_change = True
    if "color" in data or create:
        widget.color = data.get("color") or ""
    if "size" in data or create:
        widget.size = data.get("size") or "small"
    if "config" in data or create:
        widget.config = data.get("config") or {}
    if data.get("is_shared") is not None:
        widget.is_shared = bool(data["is_shared"])
    for attr, key, default in (("grid_x", "grid_x", 0), ("grid_y", "grid_y", 0), ("grid_w", "grid_w", None), ("grid_h", "grid_h", None)):
        if data.get(key) is not None:
            setattr(widget, attr, int(data[key]))
        elif create and getattr(widget, attr, None) in (None, 0) and default is not None:
            setattr(widget, attr, default)
    if create:
        if widget.grid_w in (None, 0):
            widget.grid_w = 6 if display_type in {"chart", "table", "shortcuts"} else 3
        if widget.grid_h in (None, 0):
            widget.grid_h = 8 if display_type in {"table", "shortcuts"} else (5 if display_type == "chart" else 3)


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def widgets_collection(request):
    oid = org_id(request)
    user_id = request.user.id
    if request.method == "GET":
        require_perm(request, "analytics", "read")
        rows = _visible(oid, user_id).order_by("display_order", "created_at")
        return success({"widgets": [widget_payload(w, user_id) for w in rows]})
    require_perm(request, "analytics", "write")
    data = request_body(request)
    if not (data.get("name") or "").strip():
        return error("Name is required", http_status=400)
    max_order = _visible(oid, user_id).filter(user_id=user_id).aggregate(m=Max("display_order"))["m"] or 0
    widget = Widget(organization_id=oid, user_id=user_id, display_order=max_order + 1)
    _apply_widget_fields(widget, data, create=True)
    widget.save()
    return success(widget_payload(widget, user_id))


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def widget_detail(request, widget_id):
    oid = org_id(request)
    user_id = request.user.id
    widget = _get_widget(oid, user_id, widget_id)
    if request.method == "GET":
        require_perm(request, "analytics", "read")
        return success(widget_payload(widget, user_id))
    require_perm(request, "analytics", "write")
    if str(widget.user_id) != str(user_id):
        return error("Only the widget owner can edit this widget", http_status=403)
    if request.method == "DELETE":
        soft_delete(widget)
        return success({"message": "Widget deleted successfully"})
    _apply_widget_fields(widget, request_body(request))
    widget.save()
    return success(widget_payload(widget, user_id))


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def save_layout(request):
    oid = org_id(request)
    user_id = request.user.id
    layout = request_body(request).get("layout") or []
    if not layout:
        return error("Layout is required", http_status=400)
    for i, item in enumerate(layout):
        Widget.objects.filter(id=item.get("id"), organization_id=oid).filter(Q(user_id=user_id) | Q(is_shared=True)).update(
            grid_x=item.get("grid_x") or 0,
            grid_y=item.get("grid_y") or 0,
            grid_w=item.get("grid_w") or 0,
            grid_h=item.get("grid_h") or 0,
            display_order=i,
        )
    return success({"message": "Layout saved successfully"})


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def data_sources(request):
    org_id(request)
    sources = [{"name": name, "label": name.replace("_", " ").title(), "fields": fields} for name, fields in DATA_SOURCES.items()]
    return success(
        {
            "data_sources": sources,
            "metrics": METRICS,
            "display_types": DISPLAY_TYPES,
            "operators": [
                {"value": "equals", "label": "Equals"},
                {"value": "not_equals", "label": "Not Equals"},
                {"value": "contains", "label": "Contains"},
                {"value": "gt", "label": "Greater Than"},
                {"value": "lt", "label": "Less Than"},
                {"value": "gte", "label": "Greater Than or Equal"},
                {"value": "lte", "label": "Less Than or Equal"},
            ],
        }
    )


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def widget_data(request, widget_id):
    oid = org_id(request)
    widget = _get_widget(oid, request.user.id, widget_id)
    data = execute_widget(oid, widget, request.query_params.get("from") or "", request.query_params.get("to") or "")
    return success(data)


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def all_widgets_data(request):
    oid = org_id(request)
    user_id = request.user.id
    from_str = request.query_params.get("from") or ""
    to_str = request.query_params.get("to") or ""
    results = {}
    for widget in _visible(oid, user_id).order_by("display_order"):
        results[str(widget.id)] = execute_widget(oid, widget, from_str, to_str)
    return success({"data": results})
