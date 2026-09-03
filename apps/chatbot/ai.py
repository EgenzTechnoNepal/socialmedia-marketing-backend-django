import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

from apps.billing.entitlements import FEATURE_AI, METER_AI_COMPLETION, assert_feature, record_usage
from apps.chatbot.models import AIContext, ChatbotSessionMessage
from services.crypto import decrypt
from services.templates import process_template

logger = logging.getLogger(__name__)


def decrypt_ai_key(raw: str) -> str:
    return decrypt(raw or "", settings.ENCRYPTION_KEY or "")


def generate_ai_response(chatbot_settings, session, user_message: str) -> str:
    assert_feature(chatbot_settings.organization_id, FEATURE_AI)
    context_data = build_ai_context(chatbot_settings.organization_id, session, user_message)
    provider = (chatbot_settings.ai_provider or "").lower()
    api_key = decrypt_ai_key(chatbot_settings.ai_api_key)
    if not api_key:
        raise RuntimeError("AI API key is not configured")
    if provider == "openai":
        text = _openai(chatbot_settings, session, user_message, context_data, api_key)
    elif provider == "anthropic":
        text = _anthropic(chatbot_settings, session, user_message, context_data, api_key)
    elif provider == "google":
        text = _google(chatbot_settings, session, user_message, context_data, api_key)
    else:
        raise RuntimeError(f"unsupported AI provider: {chatbot_settings.ai_provider}")
    if text:
        record_usage(
            chatbot_settings.organization_id,
            METER_AI_COMPLETION,
            event_id=f"ai:{session.id if session else 'adhoc'}:{hash(user_message) & 0xFFFFFFFF}",
        )
    return text


def build_ai_context(org_id, session, user_message: str) -> str:
    account = session.whatsapp_account if session else ""
    contexts = list(
        AIContext.objects.filter(organization_id=org_id, is_enabled=True)
        .filter(whatsapp_account__in=[account, ""])
        .order_by("-priority", "-created_at")
    )
    parts = []
    for ctx in contexts:
        content = ctx.static_content or ""
        if ctx.context_type == "api" and ctx.api_config:
            try:
                api_content = _fetch_api_context(ctx.api_config, session, user_message)
                if api_content:
                    content = f"{content}\n\nData:\n{api_content}" if content else api_content
            except Exception:
                logger.exception("AI context API fetch failed for %s", ctx.name)
        if content:
            parts.append(f"### {ctx.name}\n{content}")
    if not parts:
        return ""
    return "## Context Information\n\n" + "\n\n".join(parts)


def _history(session, limit: int):
    if not session:
        return []
    qs = ChatbotSessionMessage.objects.filter(session=session).order_by("-created_at")[: max(limit or 0, 0)]
    return list(reversed(list(qs)))


def _system_prompt(settings, context_data: str) -> str:
    prompt = settings.ai_system_prompt or ""
    if context_data:
        prompt = f"{prompt}\n\n{context_data}" if prompt else context_data
    return prompt


def _openai(settings, session, user_message, context_data, api_key):
    messages = []
    system = _system_prompt(settings, context_data)
    if system:
        messages.append({"role": "system", "content": system})
    if settings.ai_include_history and session:
        for msg in _history(session, settings.ai_history_limit or 4):
            messages.append(
                {"role": "assistant" if msg.direction == "outgoing" else "user", "content": msg.message or ""}
            )
    messages.append({"role": "user", "content": user_message})
    payload = {"model": settings.ai_model, "messages": messages, "max_tokens": settings.ai_max_tokens or 500}
    temp = float(settings.ai_temperature or 0)
    if temp > 0:
        payload["temperature"] = temp
    body = _http_json(
        "https://api.openai.com/v1/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}"},
    )
    choices = body.get("choices") or []
    if not choices:
        return ""
    return ((choices[0].get("message") or {}).get("content") or "").strip()


def _anthropic(settings, session, user_message, context_data, api_key):
    messages = []
    if settings.ai_include_history and session:
        for msg in _history(session, settings.ai_history_limit or 4):
            messages.append(
                {"role": "assistant" if msg.direction == "outgoing" else "user", "content": msg.message or ""}
            )
    messages.append({"role": "user", "content": user_message})
    payload = {"model": settings.ai_model, "messages": messages, "max_tokens": settings.ai_max_tokens or 500}
    system = _system_prompt(settings, context_data)
    if system:
        payload["system"] = system
    temp = float(settings.ai_temperature or 0)
    if temp > 0:
        payload["temperature"] = temp
    body = _http_json(
        "https://api.anthropic.com/v1/messages",
        payload,
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    parts = body.get("content") or []
    texts = [p.get("text") for p in parts if isinstance(p, dict) and p.get("text")]
    return "\n".join(texts).strip()


def _google(settings, session, user_message, context_data, api_key):
    contents = []
    if settings.ai_include_history and session:
        for msg in _history(session, settings.ai_history_limit or 4):
            role = "model" if msg.direction == "outgoing" else "user"
            contents.append({"role": role, "parts": [{"text": msg.message or ""}]})
    contents.append({"role": "user", "parts": [{"text": user_message}]})
    payload = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": settings.ai_max_tokens or 500},
    }
    system = _system_prompt(settings, context_data)
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    temp = float(settings.ai_temperature or 0)
    if temp > 0:
        payload["generationConfig"]["temperature"] = temp
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{settings.ai_model}:generateContent?key={api_key}"
    )
    body = _http_json(url, payload, {})
    cands = body.get("candidates") or []
    if not cands:
        return ""
    parts = ((cands[0].get("content") or {}).get("parts") or [])
    return "".join(p.get("text") or "" for p in parts if isinstance(p, dict)).strip()


def _fetch_api_context(api_config, session, user_message: str) -> str:
    data = dict(session.session_data or {}) if session else {}
    if session:
        data["phone_number"] = session.phone_number
    data["user_message"] = user_message
    body, status, _ = execute_configured_api(api_config, lambda s: process_template(s, data))
    if status < 200 or status >= 300:
        return ""
    try:
        parsed = json.loads(body.decode("utf-8"))
        return json.dumps(parsed, indent=2)[:4000]
    except Exception:
        return body.decode("utf-8", errors="replace")[:4000]


def execute_configured_api(api_config: dict, replace_var):
    url = replace_var((api_config or {}).get("url") or "")
    if not url:
        raise RuntimeError("API URL is required")
    method = ((api_config.get("method") if api_config else None) or "GET").upper()
    raw_body = api_config.get("body") if api_config else None
    data = replace_var(raw_body).encode("utf-8") if isinstance(raw_body, str) and raw_body else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    extra = (api_config or {}).get("headers") or {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(value, str):
                headers[key] = replace_var(value)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()[: 1024 * 1024], resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.read()[: 1024 * 1024], exc.code, {}


def _http_json(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.warning("AI provider error %s: %s", url, detail)
        raise RuntimeError(detail or f"AI API {exc.code}") from exc
