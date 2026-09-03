from django.conf import settings as dj_settings
from django.db.models import Q
from django.utils import timezone as dj_tz
from rest_framework.decorators import api_view, permission_classes

from apps.billing.entitlements import FEATURE_AI, assert_feature
from apps.chatbot.ai import generate_ai_response
from apps.chatbot.models import (
    AIContext,
    AgentTransfer,
    ChatbotFlow,
    ChatbotFlowStep,
    ChatbotSession,
    ChatbotSessionMessage,
    ChatbotSettings,
    KeywordRule,
)
from apps.chatbot.processor import load_settings
from apps.common.envelope import error, success
from apps.common.http import iso, list_payload, org_id, parse_pagination, request_body, require_perm, soft_delete
from apps.common.permissions import CookieAuthenticated
from services.crypto import encrypt


def _maps(value):
    if not value:
        return []
    return [item for item in value if isinstance(item, dict)]


def settings_payload(row: ChatbotSettings | None) -> dict:
    if row is None:
        return {
            "enabled": False,
            "greeting_message": "Hello! How can I help you today?",
            "greeting_buttons": [],
            "fallback_message": "",
            "fallback_buttons": [],
            "session_timeout_minutes": 30,
            "business_hours_enabled": False,
            "business_hours": [],
            "out_of_hours_message": "",
            "allow_automated_outside_hours": True,
            "allow_agent_queue_pickup": True,
            "assign_to_same_agent": True,
            "agent_current_conversation_only": False,
            "ai_enabled": False,
            "ai_provider": "",
            "ai_model": "",
            "ai_max_tokens": 500,
            "ai_system_prompt": "",
            "sla_enabled": False,
            "sla_response_minutes": 15,
            "sla_resolution_minutes": 60,
            "sla_escalation_minutes": 30,
            "sla_auto_close_hours": 24,
            "sla_auto_close_message": "",
            "sla_warning_message": "",
            "sla_escalation_notify_ids": [],
            "client_reminder_enabled": False,
            "client_reminder_minutes": 30,
            "client_reminder_message": "",
            "client_auto_close_minutes": 60,
            "client_auto_close_message": "",
        }
    return {
        "enabled": row.is_enabled,
        "greeting_message": row.default_response or "",
        "greeting_buttons": _maps(row.greeting_buttons),
        "fallback_message": row.fallback_message or "",
        "fallback_buttons": _maps(row.fallback_buttons),
        "session_timeout_minutes": row.session_timeout_mins or 30,
        "business_hours_enabled": row.business_hours_enabled,
        "business_hours": _maps(row.business_hours),
        "out_of_hours_message": row.out_of_hours_message or "",
        "allow_automated_outside_hours": row.allow_automated_outside_hours,
        "allow_agent_queue_pickup": row.allow_agent_queue_pickup,
        "assign_to_same_agent": row.assign_to_same_agent,
        "agent_current_conversation_only": row.agent_current_conversation_only,
        "ai_enabled": row.ai_enabled,
        "ai_provider": row.ai_provider or "",
        "ai_model": row.ai_model or "",
        "ai_max_tokens": row.ai_max_tokens or 500,
        "ai_system_prompt": row.ai_system_prompt or "",
        "sla_enabled": row.sla_enabled,
        "sla_response_minutes": row.sla_response_minutes or 15,
        "sla_resolution_minutes": row.sla_resolution_minutes or 60,
        "sla_escalation_minutes": row.sla_escalation_minutes or 30,
        "sla_auto_close_hours": row.sla_auto_close_hours or 24,
        "sla_auto_close_message": row.sla_auto_close_message or "",
        "sla_warning_message": row.sla_warning_message or "",
        "sla_escalation_notify_ids": row.sla_escalation_notify_ids or [],
        "client_reminder_enabled": row.client_reminder_enabled,
        "client_reminder_minutes": row.client_reminder_minutes or 30,
        "client_reminder_message": row.client_reminder_message or "",
        "client_auto_close_minutes": row.client_auto_close_minutes or 60,
        "client_auto_close_message": row.client_auto_close_message or "",
    }


def chatbot_stats(oid) -> dict:
    return {
        "total_sessions": ChatbotSession.objects.filter(organization_id=oid).count(),
        "active_sessions": ChatbotSession.objects.filter(organization_id=oid, status="active").count(),
        "messages_handled": ChatbotSessionMessage.objects.filter(session__organization_id=oid).count(),
        "ai_responses": 0,
        "agent_transfers": AgentTransfer.objects.filter(organization_id=oid).count(),
        "keywords_count": KeywordRule.objects.filter(organization_id=oid).count(),
        "flows_count": ChatbotFlow.objects.filter(organization_id=oid).count(),
        "ai_contexts_count": AIContext.objects.filter(organization_id=oid).count(),
    }


@api_view(["GET", "PUT"])
@permission_classes([CookieAuthenticated])
def chatbot_settings(request):
    oid = org_id(request)
    row = ChatbotSettings.objects.filter(organization_id=oid, whatsapp_account="").first()
    if request.method == "GET":
        return success({"settings": settings_payload(row), "stats": chatbot_stats(oid)})
    data = request_body(request)
    if row is None:
        row = ChatbotSettings(organization_id=oid, whatsapp_account="")
    mapping = {
        "enabled": "is_enabled",
        "greeting_message": "default_response",
        "greeting_buttons": "greeting_buttons",
        "fallback_message": "fallback_message",
        "fallback_buttons": "fallback_buttons",
        "session_timeout_minutes": "session_timeout_mins",
        "business_hours_enabled": "business_hours_enabled",
        "business_hours": "business_hours",
        "out_of_hours_message": "out_of_hours_message",
        "allow_automated_outside_hours": "allow_automated_outside_hours",
        "allow_agent_queue_pickup": "allow_agent_queue_pickup",
        "assign_to_same_agent": "assign_to_same_agent",
        "agent_current_conversation_only": "agent_current_conversation_only",
        "ai_enabled": "ai_enabled",
        "ai_provider": "ai_provider",
        "ai_model": "ai_model",
        "ai_max_tokens": "ai_max_tokens",
        "ai_system_prompt": "ai_system_prompt",
        "sla_enabled": "sla_enabled",
        "sla_response_minutes": "sla_response_minutes",
        "sla_resolution_minutes": "sla_resolution_minutes",
        "sla_escalation_minutes": "sla_escalation_minutes",
        "sla_auto_close_hours": "sla_auto_close_hours",
        "sla_auto_close_message": "sla_auto_close_message",
        "sla_warning_message": "sla_warning_message",
        "sla_escalation_notify_ids": "sla_escalation_notify_ids",
        "client_reminder_enabled": "client_reminder_enabled",
        "client_reminder_minutes": "client_reminder_minutes",
        "client_reminder_message": "client_reminder_message",
        "client_auto_close_minutes": "client_auto_close_minutes",
        "client_auto_close_message": "client_auto_close_message",
    }
    for src, dest in mapping.items():
        if src in data and data[src] is not None:
            setattr(row, dest, data[src])
    if data.get("ai_api_key"):
        row.ai_api_key = encrypt(data["ai_api_key"], dj_settings.ENCRYPTION_KEY or "")
    row.save()
    return success({"message": "Settings updated successfully"})


def keyword_payload(rule: KeywordRule) -> dict:
    return {
        "id": str(rule.id),
        "name": rule.name,
        "keywords": rule.keywords or [],
        "match_type": rule.match_type,
        "response_type": rule.response_type,
        "response_content": rule.response_content or {},
        "priority": rule.priority,
        "enabled": rule.is_enabled,
        "created_by_name": rule.created_by.full_name if rule.created_by else "",
        "updated_by_name": rule.updated_by.full_name if rule.updated_by else "",
        "created_at": iso(rule.created_at),
        "updated_at": iso(rule.updated_at),
    }


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def keywords_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        page, limit, offset = parse_pagination(request)
        qs = KeywordRule.objects.filter(organization_id=oid)
        search = request.query_params.get("search") or ""
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(keywords__icontains=search))
        total = qs.count()
        rows = list(qs.select_related("created_by", "updated_by").order_by("-priority", "-created_at")[offset : offset + limit])
        return success(list_payload("rules", [keyword_payload(r) for r in rows], total, page, limit))
    data = request_body(request)
    keywords = data.get("keywords") or []
    if not keywords:
        return error("At least one keyword is required", http_status=400)
    rule = KeywordRule.objects.create(
        organization_id=oid,
        name=data.get("name") or keywords[0],
        keywords=keywords,
        match_type=data.get("match_type") or "contains",
        response_type=data.get("response_type") or "text",
        response_content=data.get("response_content") or {},
        priority=data.get("priority") or 0,
        is_enabled=bool(data.get("enabled")),
        created_by=request.user,
        updated_by=request.user,
        whatsapp_account="",
    )
    return success({"id": str(rule.id), "message": "Keyword rule created successfully"})


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def keyword_detail(request, rule_id):
    oid = org_id(request)
    rule = KeywordRule.objects.filter(id=rule_id, organization_id=oid).select_related("created_by", "updated_by").first()
    if not rule:
        return error("Keyword rule not found", http_status=404)
    if request.method == "GET":
        return success(keyword_payload(rule))
    if request.method == "DELETE":
        soft_delete(rule)
        return success({"message": "Keyword rule deleted successfully"})
    data = request_body(request)
    if data.get("name") is not None:
        rule.name = data["name"]
    if data.get("keywords"):
        rule.keywords = data["keywords"]
    if data.get("match_type") is not None:
        rule.match_type = data["match_type"]
    if data.get("response_type") is not None:
        rule.response_type = data["response_type"]
    if data.get("response_content") is not None:
        rule.response_content = data["response_content"]
    if data.get("priority") is not None:
        rule.priority = data["priority"]
    if data.get("enabled") is not None:
        rule.is_enabled = data["enabled"]
    rule.updated_by = request.user
    rule.save()
    return success({"message": "Keyword rule updated successfully"})


def flow_list_payload(flow: ChatbotFlow) -> dict:
    return {
        "id": str(flow.id),
        "name": flow.name,
        "description": flow.description or "",
        "trigger_keywords": flow.trigger_keywords or [],
        "enabled": flow.is_enabled,
        "created_at": iso(flow.created_at),
    }


def flow_full_payload(flow: ChatbotFlow) -> dict:
    return {
        "id": str(flow.id),
        "organization_id": str(flow.organization_id),
        "whatsapp_account": flow.whatsapp_account or "",
        "name": flow.name,
        "is_enabled": flow.is_enabled,
        "description": flow.description or "",
        "trigger_keywords": flow.trigger_keywords or [],
        "trigger_button_id": flow.trigger_button_id or "",
        "initial_message": flow.initial_message or "",
        "initial_message_type": flow.initial_message_type or "text",
        "completion_message": flow.completion_message or "",
        "on_complete_action": flow.on_complete_action or "",
        "completion_config": flow.completion_config or {},
        "timeout_message": flow.timeout_message or "",
        "cancel_keywords": flow.cancel_keywords or [],
        "panel_config": flow.panel_config or {},
        "graph": flow.graph,
        "created_at": iso(flow.created_at),
        "updated_at": iso(flow.updated_at),
    }


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def flows_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        require_perm(request, "flows.chatbot", "read")
        page, limit, offset = parse_pagination(request)
        qs = ChatbotFlow.objects.filter(organization_id=oid)
        search = request.query_params.get("search") or ""
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(description__icontains=search))
        total = qs.count()
        rows = list(qs.order_by("-created_at")[offset : offset + limit])
        return success(list_payload("flows", [flow_list_payload(f) for f in rows], total, page, limit))
    require_perm(request, "flows.chatbot", "write")
    data = request_body(request)
    if not data.get("name"):
        return error("Name is required", http_status=400)
    flow = ChatbotFlow.objects.create(
        organization_id=oid,
        name=data["name"],
        description=data.get("description") or "",
        trigger_keywords=data.get("trigger_keywords") or [],
        initial_message=data.get("initial_message") or "",
        completion_message=data.get("completion_message") or "",
        on_complete_action=data.get("on_complete_action") or "",
        completion_config=data.get("completion_config") or {},
        panel_config=data.get("panel_config") or {},
        graph=data.get("graph"),
        is_enabled=bool(data.get("enabled")),
        created_by=request.user,
        updated_by=request.user,
        whatsapp_account="",
    )
    return success({"id": str(flow.id), "message": "Flow created successfully"})


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def flow_detail(request, flow_id):
    oid = org_id(request)
    flow = ChatbotFlow.objects.filter(id=flow_id, organization_id=oid).first()
    if request.method == "GET":
        require_perm(request, "flows.chatbot", "read")
        if not flow:
            return error("Flow not found", http_status=404)
        return success(flow_full_payload(flow))
    if request.method == "DELETE":
        require_perm(request, "flows.chatbot", "delete")
        if not flow:
            return error("Flow not found", http_status=404)
        ChatbotFlowStep.objects.filter(flow=flow).update(deleted_at=dj_tz.now())
        soft_delete(flow)
        return success({"message": "Flow deleted successfully"})
    require_perm(request, "flows.chatbot", "write")
    if not flow:
        return error("Flow not found", http_status=404)
    data = request_body(request)
    for src, dest in (
        ("name", "name"),
        ("description", "description"),
        ("initial_message", "initial_message"),
        ("completion_message", "completion_message"),
        ("on_complete_action", "on_complete_action"),
        ("completion_config", "completion_config"),
        ("panel_config", "panel_config"),
        ("graph", "graph"),
    ):
        if src in data and data[src] is not None:
            setattr(flow, dest, data[src])
    if data.get("trigger_keywords"):
        flow.trigger_keywords = data["trigger_keywords"]
    if data.get("enabled") is not None:
        flow.is_enabled = data["enabled"]
    flow.updated_by = request.user
    flow.save()
    return success({"message": "Flow updated successfully"})


def ai_context_payload(ctx: AIContext) -> dict:
    return {
        "id": str(ctx.id),
        "name": ctx.name,
        "context_type": ctx.context_type,
        "trigger_keywords": ctx.trigger_keywords or [],
        "static_content": ctx.static_content or "",
        "api_config": ctx.api_config or {},
        "enabled": ctx.is_enabled,
        "priority": ctx.priority,
        "created_by_name": ctx.created_by.full_name if ctx.created_by else "",
        "updated_by_name": ctx.updated_by.full_name if ctx.updated_by else "",
        "created_at": iso(ctx.created_at),
        "updated_at": iso(ctx.updated_at),
    }


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def ai_contexts_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        page, limit, offset = parse_pagination(request)
        qs = AIContext.objects.filter(organization_id=oid)
        search = request.query_params.get("search") or ""
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(static_content__icontains=search))
        total = qs.count()
        rows = list(qs.select_related("created_by", "updated_by").order_by("-priority", "-created_at")[offset : offset + limit])
        return success(list_payload("contexts", [ai_context_payload(c) for c in rows], total, page, limit))
    data = request_body(request)
    if not data.get("name"):
        return error("Name is required", http_status=400)
    ctx = AIContext.objects.create(
        organization_id=oid,
        name=data["name"],
        context_type=data.get("context_type") or "static",
        trigger_keywords=data.get("trigger_keywords") or [],
        static_content=data.get("static_content") or "",
        api_config=data.get("api_config") or {},
        priority=data.get("priority") or 0,
        is_enabled=bool(data.get("enabled")),
        created_by=request.user,
        updated_by=request.user,
        whatsapp_account="",
    )
    return success({"id": str(ctx.id), "message": "AI context created successfully"})


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def ai_context_detail(request, ctx_id):
    oid = org_id(request)
    ctx = AIContext.objects.filter(id=ctx_id, organization_id=oid).select_related("created_by", "updated_by").first()
    if not ctx:
        return error("AI context not found", http_status=404)
    if request.method == "GET":
        return success(ai_context_payload(ctx))
    if request.method == "DELETE":
        soft_delete(ctx)
        return success({"message": "AI context deleted successfully"})
    data = request_body(request)
    if data.get("name") is not None:
        ctx.name = data["name"]
    if data.get("context_type") is not None:
        ctx.context_type = data["context_type"]
    if data.get("trigger_keywords"):
        ctx.trigger_keywords = data["trigger_keywords"]
    if data.get("static_content") is not None:
        ctx.static_content = data["static_content"]
    if data.get("api_config") is not None:
        ctx.api_config = data["api_config"]
    if data.get("priority") is not None:
        ctx.priority = data["priority"]
    if data.get("enabled") is not None:
        ctx.is_enabled = data["enabled"]
    ctx.updated_by = request.user
    ctx.save()
    return success({"message": "AI context updated successfully"})


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def sessions_collection(request):
    oid = org_id(request)
    qs = ChatbotSession.objects.filter(organization_id=oid).select_related("contact").order_by("-last_activity_at")
    status = request.query_params.get("status") or ""
    if status:
        qs = qs.filter(status=status)
    rows = list(qs[:100])
    return success({"sessions": [_session_payload(s) for s in rows]})


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def session_detail(request, session_id):
    oid = org_id(request)
    session = ChatbotSession.objects.filter(id=session_id, organization_id=oid).select_related("contact").first()
    if not session:
        return error("Session not found", http_status=404)
    payload = _session_payload(session)
    payload["messages"] = [
        {
            "id": str(m.id),
            "direction": m.direction,
            "message": m.message,
            "step_name": m.step_name,
            "created_at": iso(m.created_at),
        }
        for m in ChatbotSessionMessage.objects.filter(session=session).order_by("created_at")
    ]
    return success(payload)


def _session_payload(session: ChatbotSession) -> dict:
    return {
        "id": str(session.id),
        "organization_id": str(session.organization_id),
        "contact_id": str(session.contact_id),
        "whatsapp_account": session.whatsapp_account,
        "phone_number": session.phone_number,
        "status": session.status,
        "current_flow_id": str(session.current_flow_id) if session.current_flow_id else None,
        "current_step": session.current_step or "",
        "session_data": session.session_data or {},
        "started_at": iso(session.started_at),
        "last_activity_at": iso(session.last_activity_at),
        "completed_at": iso(session.completed_at),
    }


@api_view(["POST"])
@permission_classes([CookieAuthenticated])
def generate_ai(request):
    oid = org_id(request)
    assert_feature(oid, FEATURE_AI)
    data = request_body(request)
    settings_row = load_settings(oid, "")
    if not settings_row or not settings_row.ai_enabled:
        return error("AI is not enabled", http_status=400)
    session = None
    if data.get("session_id"):
        session = ChatbotSession.objects.filter(id=data["session_id"], organization_id=oid).first()
    try:
        text = generate_ai_response(settings_row, session, data.get("prompt") or data.get("message") or "")
    except Exception as exc:
        return error(str(exc), http_status=502)
    return success({"text": text})
