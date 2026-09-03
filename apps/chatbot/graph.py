from __future__ import annotations

import ast
import logging
import operator
import re
from dataclasses import dataclass, field

from django.utils import timezone as dj_tz

from apps.chatbot.ai import execute_configured_api, generate_ai_response
from apps.chatbot.models import ChatbotFlow, ChatbotSession, WhatsAppFlow
from apps.messaging.outbound import send_and_save_buttons, send_and_save_flow, send_and_save_text
from services.templates import extract_mapping, process_template

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 100


@dataclass
class ChatNode:
    id: str
    type: str
    label: str = ""
    config: dict = field(default_factory=dict)


@dataclass
class ChatGraph:
    version: int
    entry_node: str
    nodes: dict[str, ChatNode]
    edges: dict[str, list[tuple[str, str]]]

    def node(self, node_id: str) -> ChatNode | None:
        return self.nodes.get(node_id)

    def resolve(self, from_id: str, outcome: str) -> str:
        default = ""
        for condition, to_id in self.edges.get(from_id) or []:
            if condition == outcome:
                return to_id
            if condition == "default":
                default = to_id
        return default


@dataclass
class NodeOutcome:
    outcome: str = ""
    yield_: bool = False


class GraphContext:
    def __init__(self, account, contact, session: ChatbotSession, user_input, button_id, flow_response):
        self.account = account
        self.contact = contact
        self.session = session
        self.user_input = user_input or ""
        self.button_id = button_id or ""
        self.flow_response = flow_response or {}
        self.consumed = False


def parse_chat_graph(raw) -> ChatGraph | None:
    if not raw:
        return None
    version = raw.get("version")
    if version != 2:
        raise ValueError(f"unsupported graph version {version} (want 2)")
    entry = raw.get("entry_node") or ""
    if not entry:
        raise ValueError("graph missing entry_node")
    nodes = {}
    for item in raw.get("nodes") or []:
        node = ChatNode(
            id=item.get("id") or "",
            type=item.get("type") or "",
            label=item.get("label") or "",
            config=item.get("config") or {},
        )
        if node.id:
            nodes[node.id] = node
    edges: dict[str, list[tuple[str, str]]] = {}
    for edge in raw.get("edges") or []:
        frm = edge.get("from") or ""
        to = edge.get("to") or ""
        cond = edge.get("condition") or "default"
        if frm and to:
            edges.setdefault(frm, []).append((cond, to))
    if entry not in nodes:
        raise ValueError(f"entry_node {entry!r} not found in nodes")
    return ChatGraph(version=2, entry_node=entry, nodes=nodes, edges=edges)


def run_chat_graph(account, contact, session: ChatbotSession, flow: ChatbotFlow, user_input, button_id, flow_response):
    graph = parse_chat_graph(flow.graph)
    if graph is None:
        raise ValueError("flow has no v2 graph")
    ctx = GraphContext(account, contact, session, user_input, button_id, flow_response)
    data = dict(session.session_data or {})
    data["phone_number"] = session.phone_number
    if contact:
        data["contact_name"] = contact.profile_name or ""
    session.session_data = data
    if not session.current_step:
        session.current_step = graph.entry_node
        ctx.user_input = ""
        ctx.button_id = ""

    for _ in range(MAX_ITERATIONS):
        node = graph.node(session.current_step)
        if node is None:
            raise ValueError(f"node {session.current_step!r} not found")
        skip_expr = cfg_str(node.config, "skip_condition")
        if skip_expr:
            try:
                matched = evaluate_condition(skip_expr, session.session_data or {})
            except Exception:
                matched = False
            if matched:
                _append_path(session, node, "skipped")
                nxt = graph.resolve(node.id, "default")
                if not nxt:
                    session.status = "completed"
                    persist_session(session)
                    return
                session.current_step = nxt
                continue
        res = execute_node(node, ctx)
        _append_path(session, node, res.outcome)
        if res.yield_:
            persist_session(session)
            return
        if session.current_flow_id and str(session.current_flow_id) != str(flow.id):
            new_flow = ChatbotFlow.objects.filter(id=session.current_flow_id, organization_id=account.organization_id).first()
            if not new_flow or not new_flow.graph:
                persist_session(session)
                raise ValueError("goto_flow: target flow has no v2 graph")
            flow = new_flow
            graph = parse_chat_graph(new_flow.graph)
            session.current_step = graph.entry_node
            continue
        nxt = graph.resolve(node.id, res.outcome)
        if not nxt:
            session.status = "completed"
            persist_session(session)
            return
        session.current_step = nxt
    persist_session(session)
    raise RuntimeError("chat graph: too many non-blocking nodes in a single inbound (cycle?)")


def execute_node(node: ChatNode, ctx: GraphContext) -> NodeOutcome:
    handlers = {
        "start": lambda n, c: NodeOutcome(outcome="default"),
        "message": _exec_message,
        "buttons": _exec_buttons,
        "prompt": _exec_prompt,
        "api_call": _exec_api_call,
        "condition": _exec_condition,
        "timing": _exec_timing,
        "set_variable": _exec_set_variable,
        "ai_response": _exec_ai,
        "transfer": _exec_transfer,
        "webhook": _exec_webhook,
        "goto_flow": _exec_goto,
        "whatsapp_flow": _exec_wa_flow,
        "end": _exec_end,
    }
    handler = handlers.get(node.type)
    if not handler:
        raise ValueError(f"chat node type {node.type!r} not implemented")
    return handler(node, ctx)


def _exec_message(node, ctx):
    text = process_template(cfg_str(node.config, "message", "text"), ctx.session.session_data)
    if text:
        send_and_save_text(ctx.account, ctx.contact, text)
        log_session_message(ctx.session, "outgoing", text, node.id)
    return NodeOutcome(outcome="default")


def _exec_buttons(node, ctx):
    if not ctx.consumed and ctx.button_id:
        ctx.consumed = True
        store_as = cfg_str(node.config, "store_as")
        if store_as:
            data = dict(ctx.session.session_data or {})
            data[store_as] = ctx.user_input or ctx.button_id
            ctx.session.session_data = data
        return NodeOutcome(outcome=f"button:{ctx.button_id}")
    body = process_template(cfg_str(node.config, "body", "message", "text") or node.label, ctx.session.session_data)
    buttons = []
    for item in node.config.get("buttons") or []:
        if not isinstance(item, dict):
            continue
        btn = dict(item)
        for key in ("title", "url", "phone_number"):
            if isinstance(btn.get(key), str):
                btn[key] = process_template(btn[key], ctx.session.session_data)
        buttons.append(btn)
    if not buttons:
        raise ValueError(f"buttons node {node.id!r} has no buttons configured")
    send_and_save_buttons(ctx.account, ctx.contact, body, buttons)
    log_session_message(ctx.session, "outgoing", body, node.id)
    return NodeOutcome(yield_=True)


def _exec_prompt(node, ctx):
    body = cfg_str(node.config, "body", "message", "text")
    if not ctx.consumed and not ctx.user_input:
        if not body:
            raise ValueError(f"prompt node {node.id!r} has no body configured")
        rendered = process_template(body, ctx.session.session_data)
        send_and_save_text(ctx.account, ctx.contact, rendered)
        log_session_message(ctx.session, "outgoing", rendered, node.id)
        return NodeOutcome(yield_=True)
    if ctx.consumed:
        return NodeOutcome(yield_=True)
    ctx.consumed = True
    regex = cfg_str(node.config, "validation_regex")
    if regex:
        try:
            if not re.search(regex, ctx.user_input):
                return _prompt_invalid(node, ctx)
        except re.error:
            logger.exception("invalid prompt regex on node %s", node.id)
    store_as = cfg_str(node.config, "store_as")
    if store_as:
        data = dict(ctx.session.session_data or {})
        data[store_as] = ctx.user_input
        ctx.session.session_data = data
    ctx.session.step_retries = 0
    return NodeOutcome(outcome="default")


def _prompt_invalid(node, ctx):
    ctx.session.step_retries = (ctx.session.step_retries or 0) + 1
    max_retries = cfg_int(node.config, "max_retries", 3)
    if ctx.session.step_retries >= max_retries:
        ctx.session.step_retries = 0
        return NodeOutcome(outcome="max_retries")
    error_msg = process_template(
        cfg_str(node.config, "validation_error") or "Invalid input. Please try again.",
        ctx.session.session_data,
    )
    send_and_save_text(ctx.account, ctx.contact, error_msg)
    log_session_message(ctx.session, "outgoing", error_msg, node.id)
    return NodeOutcome(yield_=True)


def _exec_api_call(node, ctx):
    data = dict(ctx.session.session_data or {})
    data["phone_number"] = ctx.session.phone_number
    ctx.session.session_data = data
    try:
        body, status, _ = execute_configured_api(node.config, lambda s: process_template(s, data))
    except Exception:
        logger.exception("api_call node failed")
        return NodeOutcome(outcome="http:non2xx")
    if status < 200 or status >= 300:
        return NodeOutcome(outcome="http:non2xx")
    mapping = node.config.get("response_mapping") or {}
    if isinstance(mapping, dict) and mapping:
        try:
            import json

            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict):
                data.update(extract_mapping(parsed, mapping))
                ctx.session.session_data = data
        except Exception:
            pass
    tmpl = cfg_str(node.config, "message_template")
    if tmpl:
        rendered = process_template(tmpl, data)
        if rendered:
            send_and_save_text(ctx.account, ctx.contact, rendered)
            log_session_message(ctx.session, "outgoing", rendered, node.id)
    return NodeOutcome(outcome="http:2xx")


def _exec_condition(node, ctx):
    expr = cfg_str(node.config, "expression")
    if not expr:
        return NodeOutcome(outcome="false")
    try:
        matched = evaluate_condition(expr, ctx.session.session_data or {})
    except Exception:
        matched = False
    return NodeOutcome(outcome="true" if matched else "false")


def _exec_timing(node, ctx):
    schedule = node.config.get("schedule") or []
    now = dj_tz.now()
    day_name = now.strftime("%A").lower()
    now_minutes = now.hour * 60 + now.minute
    for item in schedule:
        if not isinstance(item, dict):
            continue
        if (item.get("day") or "").lower() != day_name:
            continue
        if not item.get("enabled"):
            return NodeOutcome(outcome="out_of_hours")
        try:
            start_h, start_m = (item.get("start_time") or "00:00").split(":")[:2]
            end_h, end_m = (item.get("end_time") or "00:00").split(":")[:2]
            start = int(start_h) * 60 + int(start_m)
            end = int(end_h) * 60 + int(end_m)
        except Exception:
            return NodeOutcome(outcome="out_of_hours")
        if start <= now_minutes < end:
            return NodeOutcome(outcome="in_hours")
        return NodeOutcome(outcome="out_of_hours")
    return NodeOutcome(outcome="out_of_hours")


def _exec_set_variable(node, ctx):
    assignments = node.config.get("set") or {}
    data = dict(ctx.session.session_data or {})
    if isinstance(assignments, dict):
        for name, raw in assignments.items():
            if not name:
                continue
            data[name] = process_template(raw, data) if isinstance(raw, str) else raw
    ctx.session.session_data = data
    return NodeOutcome(outcome="default")


def _exec_ai(node, ctx):
    from apps.chatbot.processor import load_settings

    settings = load_settings(ctx.account.organization_id, ctx.account.name)
    if not settings or not settings.ai_enabled or not settings.ai_provider or not settings.ai_api_key:
        return NodeOutcome(outcome="default")
    user_message = ctx.user_input
    tmpl = cfg_str(node.config, "prompt_template", "prompt")
    if tmpl:
        user_message = process_template(tmpl, ctx.session.session_data)
    try:
        answer = generate_ai_response(settings, ctx.session, user_message)
    except Exception:
        logger.exception("ai_response node failed")
        return NodeOutcome(outcome="default")
    if not answer:
        return NodeOutcome(outcome="default")
    send_and_save_text(ctx.account, ctx.contact, answer)
    log_session_message(ctx.session, "outgoing", answer, node.id)
    return NodeOutcome(outcome="default")


def _exec_transfer(node, ctx):
    from apps.chatbot.transfers import create_transfer_to_queue, create_transfer_to_team

    body = cfg_str(node.config, "body", "message", "text")
    if body:
        message = process_template(body, ctx.session.session_data)
        send_and_save_text(ctx.account, ctx.contact, message)
        log_session_message(ctx.session, "outgoing", message, node.id)
    notes = process_template(cfg_str(node.config, "notes"), ctx.session.session_data)
    team_id = cfg_str(node.config, "team_id")
    if team_id and team_id != "_general":
        create_transfer_to_team(ctx.account, ctx.contact, team_id, notes, "flow")
    else:
        create_transfer_to_queue(ctx.account, ctx.contact, "flow")
    ctx.session.status = "completed"
    return NodeOutcome(yield_=True)


def _exec_webhook(node, ctx):
    data = dict(ctx.session.session_data or {})
    data["phone_number"] = ctx.session.phone_number
    ctx.session.session_data = data
    try:
        execute_configured_api(node.config, lambda s: process_template(s, data))
    except Exception:
        logger.exception("webhook node failed")
    return NodeOutcome(outcome="default")


def _exec_goto(node, ctx):
    target_id = cfg_str(node.config, "flow_id")
    if not target_id:
        return NodeOutcome()
    target = ChatbotFlow.objects.filter(id=target_id, organization_id=ctx.account.organization_id).first()
    if not target or not target.is_enabled or not target.graph:
        return NodeOutcome()
    if target.whatsapp_account and target.whatsapp_account != ctx.session.whatsapp_account:
        return NodeOutcome()
    data = dict(ctx.session.session_data or {})
    path = list(data.get("__path__") or [])
    path.append({"action": "goto_flow", "flow": target.name, "flow_id": str(target.id)})
    data["__path__"] = path
    ctx.session.session_data = data
    ctx.session.current_flow = target
    return NodeOutcome(outcome="goto")


def _exec_wa_flow(node, ctx):
    if not ctx.consumed and ctx.flow_response:
        ctx.consumed = True
        data = dict(ctx.session.session_data or {})
        data.update(ctx.flow_response)
        ctx.session.session_data = data
        return NodeOutcome(outcome="default")
    flow_id = cfg_str(node.config, "flow_id")
    if not flow_id:
        return NodeOutcome(outcome="default")
    body = process_template(cfg_str(node.config, "body", "message", "text"), ctx.session.session_data)
    header = process_template(cfg_str(node.config, "header"), ctx.session.session_data)
    cta = process_template(cfg_str(node.config, "cta"), ctx.session.session_data)
    first_screen = ""
    wa_flow = WhatsAppFlow.objects.filter(meta_flow_id=flow_id).first()
    if wa_flow:
        screens = wa_flow.screens or []
        if screens and isinstance(screens[0], dict):
            first_screen = screens[0].get("id") or ""
        if not first_screen and isinstance(wa_flow.flow_json, dict):
            json_screens = wa_flow.flow_json.get("screens") or []
            if json_screens and isinstance(json_screens[0], dict):
                first_screen = json_screens[0].get("id") or ""
    token = f"chatbot_{ctx.session.id}_{node.id}_{int(dj_tz.now().timestamp() * 1e9)}"
    send_and_save_flow(ctx.account, ctx.contact, flow_id, header, body, cta, token, first_screen)
    log_session_message(ctx.session, "outgoing", body, node.id)
    return NodeOutcome(yield_=True)


def _exec_end(node, ctx):
    msg = cfg_str(node.config, "message")
    if msg:
        msg = process_template(msg, ctx.session.session_data)
        send_and_save_text(ctx.account, ctx.contact, msg)
        log_session_message(ctx.session, "outgoing", msg, node.id)
    return NodeOutcome()


def persist_session(session: ChatbotSession):
    session.last_activity_at = dj_tz.now()
    if session.status == "completed" and session.completed_at is None:
        session.completed_at = dj_tz.now()
    session.save()


def log_session_message(session, direction, message, step_name):
    from apps.chatbot.models import ChatbotSessionMessage

    ChatbotSessionMessage.objects.create(session=session, direction=direction, message=message or "", step_name=step_name or "")


def _append_path(session, node: ChatNode, outcome: str):
    data = dict(session.session_data or {})
    path = list(data.get("__path__") or [])
    path.append({"node": node.id, "type": node.type, "label": node.label, "outcome": outcome})
    data["__path__"] = path
    session.session_data = data


def cfg_str(cfg: dict, *keys) -> str:
    for key in keys:
        value = (cfg or {}).get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def cfg_int(cfg: dict, key: str, default: int) -> int:
    value = (cfg or {}).get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.And: lambda a, b: a and b,
    ast.Or: lambda a, b: a or b,
}


def evaluate_condition(expression: str, data: dict) -> bool:
    tree = ast.parse(expression, mode="eval")

    def eval_node(node):
        if isinstance(node, ast.Expression):
            return eval_node(node.body)
        if isinstance(node, ast.BoolOp):
            values = [eval_node(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            return any(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not eval_node(node.operand)
        if isinstance(node, ast.Compare):
            left = eval_node(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = eval_node(comparator)
                fn = _OPS.get(type(op))
                if fn is None or not fn(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Name):
            return data.get(node.id)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Attribute):
            base = eval_node(node.value)
            if isinstance(base, dict):
                return base.get(node.attr)
            return None
        raise ValueError("unsupported expression")

    result = eval_node(tree)
    if isinstance(result, bool):
        return result
    if result is None:
        return False
    if isinstance(result, str):
        return result != "" and result.lower() != "false"
    if isinstance(result, (int, float)):
        return result != 0
    return bool(result)
