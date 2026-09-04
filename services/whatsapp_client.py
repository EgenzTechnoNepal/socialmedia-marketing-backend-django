import json
import logging
import uuid as uuidlib
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

BASE_URL = "https://graph.facebook.com"


class WhatsAppError(Exception):
    pass


def _version(account) -> str:
    return account.api_version or "v21.0"


def _messages_url(account) -> str:
    return f"{BASE_URL}/{_version(account)}/{account.phone_id}/messages"


def _request(method: str, url: str, token: str, body=None, content_type="application/json"):
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = body
        else:
            data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.warning("WhatsApp API error %s %s: %s", method, url, detail)
        raise WhatsAppError(detail or f"WhatsApp API {exc.code}") from exc


def _parse_wamid(payload: dict) -> str:
    messages = payload.get("messages") or []
    if not messages:
        raise WhatsAppError("no message ID in response")
    return messages[0].get("id") or ""


def send_text(account, phone: str, text: str, reply_to_wamid: str = "") -> str:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    if reply_to_wamid:
        payload["context"] = {"message_id": reply_to_wamid}
    return _parse_wamid(_request("POST", _messages_url(account), account.access_token, payload))


def send_media(account, phone: str, media_type: str, media_id: str, caption: str = "", filename: str = "") -> str:
    fields = {"id": media_id}
    if caption and media_type in {"image", "video", "document"}:
        fields["caption"] = caption
    if filename and media_type == "document":
        fields["filename"] = filename
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": media_type,
        media_type: fields,
    }
    return _parse_wamid(_request("POST", _messages_url(account), account.access_token, payload))


def send_template(account, phone: str, name: str, language: str, components: list) -> str:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": language},
            "components": components or [],
        },
    }
    return _parse_wamid(_request("POST", _messages_url(account), account.access_token, payload))


def send_cta_url(account, phone: str, body: str, button_text: str, url: str) -> str:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body},
            "action": {
                "name": "cta_url",
                "parameters": {"display_text": (button_text or "Open")[:20], "url": url},
            },
        },
    }
    return _parse_wamid(_request("POST", _messages_url(account), account.access_token, payload))


def send_flow(account, phone: str, flow_id: str, header: str, body: str, cta: str, flow_token: str, first_screen: str) -> str:
    interactive = {
        "type": "flow",
        "body": {"text": body or "Please complete this form"},
        "action": {
            "name": "flow",
            "parameters": {
                "flow_message_version": "3",
                "flow_token": flow_token,
                "flow_id": flow_id,
                "flow_cta": cta or "Open",
            },
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if first_screen:
        interactive["action"]["parameters"]["flow_action"] = "navigate"
        interactive["action"]["parameters"]["flow_action_payload"] = {"screen": first_screen}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "interactive",
        "interactive": interactive,
    }
    return _parse_wamid(_request("POST", _messages_url(account), account.access_token, payload))


def send_buttons(account, phone: str, body: str, buttons: list) -> str:
    button_list = []
    for btn in buttons[:3]:
        title = (btn.get("title") or "")[:20]
        button_list.append(
            {"type": "reply", "reply": {"id": btn.get("id") or title, "title": title}}
        )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": button_list},
        },
    }
    return _parse_wamid(_request("POST", _messages_url(account), account.access_token, payload))


def mark_read(account, wamid: str) -> None:
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": wamid}
    try:
        _request("POST", _messages_url(account), account.access_token, payload)
    except WhatsAppError:
        logger.exception("Failed to send WhatsApp read receipt")


def upload_media(account, data: bytes, mime_type: str, filename: str) -> str:
    url = f"{BASE_URL}/{_version(account)}/{account.phone_id}/media"
    boundary = f"----WhmBoundary{uuidlib.uuid4().hex}"
    chunks = []
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(b'Content-Disposition: form-data; name="messaging_product"\r\n\r\n')
    chunks.append(b"whatsapp\r\n")
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    chunks.append(f"Content-Type: {mime_type}\r\n\r\n".encode())
    chunks.append(data)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    payload = _request(
        "POST",
        url,
        account.access_token,
        body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    media_id = payload.get("id")
    if not media_id:
        raise WhatsAppError("no media ID in upload response")
    return media_id


def get_media_url(account, media_id: str) -> str:
    url = f"{BASE_URL}/{_version(account)}/{media_id}"
    payload = _request("GET", url, account.access_token)
    return payload.get("url") or ""


def download_url(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _graph(account, path: str) -> str:
    return f"{BASE_URL}/{_version(account)}/{path.lstrip('/')}"


def create_flow(account, name: str, categories: list) -> str:
    payload = {"name": name, "categories": categories or []}
    result = _request("POST", _graph(account, f"{account.business_id}/flows"), account.access_token, payload)
    flow_id = result.get("id") or ""
    if not flow_id:
        raise WhatsAppError("no flow id in Meta response")
    return flow_id


def update_flow_json(account, flow_id: str, flow_json: dict) -> None:
    url = _graph(account, f"{flow_id}/assets")
    json_bytes = json.dumps(flow_json).encode("utf-8")
    boundary = f"----WhmBoundary{uuidlib.uuid4().hex}"
    chunks = []
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(b'Content-Disposition: form-data; name="file"; filename="flow.json"\r\n')
    chunks.append(b"Content-Type: application/json\r\n\r\n")
    chunks.append(json_bytes)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(b'Content-Disposition: form-data; name="name"\r\n\r\nflow.json\r\n')
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(b'Content-Disposition: form-data; name="asset_type"\r\n\r\nFLOW_JSON\r\n')
    chunks.append(f"--{boundary}--\r\n".encode())
    result = _request(
        "POST",
        url,
        account.access_token,
        b"".join(chunks),
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    if not result.get("success"):
        errors = result.get("validation_errors")
        raise WhatsAppError(f"flow validation errors: {errors}" if errors else "failed to update flow JSON")


def publish_flow(account, flow_id: str) -> None:
    result = _request("POST", _graph(account, f"{flow_id}/publish"), account.access_token, {})
    if not result.get("success"):
        raise WhatsAppError("failed to publish flow")


def deprecate_flow(account, flow_id: str) -> None:
    result = _request("POST", _graph(account, f"{flow_id}/deprecate"), account.access_token, {})
    if not result.get("success"):
        raise WhatsAppError("failed to deprecate flow")


def get_flow(account, flow_id: str) -> dict:
    url = _graph(account, flow_id) + "?fields=id,name,status,categories,preview.invalidate(false)"
    return _request("GET", url, account.access_token)


def list_flows(account) -> list:
    url = _graph(account, f"{account.business_id}/flows") + "?fields=id,name,status,categories,preview.invalidate(false)"
    return (_request("GET", url, account.access_token) or {}).get("data") or []


def get_flow_assets(account, flow_id: str) -> dict | None:
    assets = (_request("GET", _graph(account, f"{flow_id}/assets"), account.access_token) or {}).get("data") or []
    download = ""
    for asset in assets:
        if asset.get("asset_type") == "FLOW_JSON":
            download = asset.get("download_url") or ""
            break
    if not download:
        return None
    raw = download_url(download, account.access_token)
    return json.loads(raw.decode("utf-8") or "{}")


def create_catalog(account, name: str) -> str:
    result = _request(
        "POST",
        _graph(account, f"{account.business_id}/owned_product_catalogs"),
        account.access_token,
        {"name": name},
    )
    catalog_id = result.get("id") or ""
    if not catalog_id:
        raise WhatsAppError("no catalog id in Meta response")
    return catalog_id


def list_catalogs(account) -> list:
    return (_request("GET", _graph(account, f"{account.business_id}/owned_product_catalogs"), account.access_token) or {}).get("data") or []


def delete_catalog(account, catalog_id: str) -> None:
    _request("DELETE", _graph(account, catalog_id), account.access_token)


def create_product(account, catalog_id: str, product: dict) -> str:
    body = {
        "name": product.get("name") or "",
        "price": str(product.get("price") or 0),
        "currency": product.get("currency") or "USD",
        "url": product.get("url") or "",
        "image_url": product.get("image_url") or "",
        "retailer_id": product.get("retailer_id") or "",
    }
    if product.get("description"):
        body["description"] = product["description"]
    result = _request("POST", _graph(account, f"{catalog_id}/products"), account.access_token, body)
    product_id = result.get("id") or ""
    if not product_id:
        raise WhatsAppError("no product id in Meta response")
    return product_id


def update_product(account, product_id: str, product: dict) -> None:
    body = {}
    if product.get("name"):
        body["name"] = product["name"]
    if product.get("price"):
        body["price"] = str(product["price"])
    if product.get("currency"):
        body["currency"] = product["currency"]
    if product.get("url"):
        body["url"] = product["url"]
    if product.get("image_url"):
        body["image_url"] = product["image_url"]
    if product.get("description"):
        body["description"] = product["description"]
    if body:
        _request("POST", _graph(account, product_id), account.access_token, body)


def delete_product(account, product_id: str) -> None:
    _request("DELETE", _graph(account, product_id), account.access_token)


def _normalize_granularity(granularity: str, analytics_type: str) -> str:
    value = granularity
    if value == "DAILY":
        value = "DAY"
    elif value == "MONTHLY":
        value = "MONTH"
    if analytics_type == "template_analytics":
        return "DAILY"
    if analytics_type in {"pricing_analytics", "call_analytics"}:
        if value == "DAY":
            return "DAILY"
        if value == "MONTH":
            return "MONTHLY"
    return value


def get_analytics(account, analytics_type: str, start: int, end: int, granularity: str, template_ids=None) -> dict:
    if analytics_type == "template_analytics":
        params = [
            f"start={start}",
            f"end={end}",
            "granularity=daily",
            "metric_types=cost,clicked,delivered,read,sent",
        ]
        ids = [tid for tid in (template_ids or []) if tid]
        if ids:
            numeric = []
            for tid in ids:
                numeric.append(tid if str(tid).isdigit() else f'"{tid}"')
            params.append("template_ids=[" + ",".join(numeric) + "]")
        url = _graph(account, f"{account.business_id}/template_analytics") + "?" + "&".join(params)
        raw = _request("GET", url, account.access_token) or {}
        points = []
        gran = "DAILY"
        for entry in raw.get("data") or []:
            gran = entry.get("granularity") or gran
            points.extend(entry.get("data_points") or [])
        return {"id": account.business_id, "template_analytics": {"granularity": gran, "data_points": points}}

    gran = _normalize_granularity(granularity, analytics_type)
    filters = [f"start({start})", f"end({end})", f"granularity({gran})"]
    if analytics_type == "pricing_analytics":
        filters.append("dimensions(PRICING_CATEGORY,PRICING_TYPE,COUNTRY)")
    if analytics_type == "call_analytics":
        filters.append("dimensions(direction)")
        filters.append("metric_types(COUNT,COST,AVERAGE_DURATION)")
    field = f"{analytics_type}." + ".".join(filters)
    url = _graph(account, account.business_id) + f"?fields={urllib.parse.quote(field, safe='(),.')}"
    raw = _request("GET", url, account.access_token) or {}
    nested = raw.get(analytics_type) or {}
    points = list(nested.get("data_points") or [])
    for entry in nested.get("data") or []:
        points.extend(entry.get("data_points") or [])
    key = {
        "analytics": "analytics",
        "pricing_analytics": "pricing_analytics",
        "call_analytics": "call_analytics",
    }.get(analytics_type, analytics_type)
    return {"id": raw.get("id") or account.business_id, key: {"granularity": nested.get("granularity") or gran, "data_points": points}}


def get_business_profile(account) -> dict:
    url = _graph(account, account.phone_id) + "/whatsapp_business_profile?fields=about,address,description,email,profile_picture_url,websites,vertical"
    raw = _request("GET", url, account.access_token) or {}
    data = raw.get("data") or []
    return data[0] if data else raw


def update_business_profile(account, payload: dict) -> dict:
    url = _graph(account, account.phone_id) + "/whatsapp_business_profile"
    _request("POST", url, account.access_token, payload)
    return get_business_profile(account)


def list_message_templates(account) -> list:
    url = _graph(account, account.business_id) + "/message_templates?limit=250"
    raw = _request("GET", url, account.access_token) or {}
    return list(raw.get("data") or [])


def upload_session_media(account, data: bytes, mime_type: str, filename: str) -> str:
    """App-scoped upload for template header handles."""
    app_id = account.app_id
    if not app_id:
        raise WhatsAppError("app_id is required to upload template media")
    start = _request(
        "POST",
        f"{BASE_URL}/{_version(account)}/{app_id}/uploads?file_length={len(data)}&file_type={urllib.parse.quote(mime_type)}",
        account.access_token,
        {},
    )
    session_id = start.get("id") or ""
    if not session_id:
        raise WhatsAppError("failed to start upload session")
    req = urllib.request.Request(
        f"{BASE_URL}/{_version(account)}/{session_id}",
        data=data,
        headers={
            "Authorization": f"OAuth {account.access_token}",
            "file_offset": "0",
            "Content-Type": mime_type or "application/octet-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise WhatsAppError(exc.read().decode("utf-8", errors="replace")) from exc
    return raw.get("h") or ""


def subscribe_waba(account) -> None:
    url = _graph(account, account.business_id) + "/subscribed_apps"
    _request("POST", url, account.access_token, {})


def test_phone(account) -> dict:
    fields = "display_phone_number,verified_name,code_verification_status,account_mode,quality_rating,messaging_limit_tier,whatsapp_business_manager_messaging_limit"
    url = _graph(account, account.phone_id) + f"?fields={fields}"
    return _request("GET", url, account.access_token) or {}


def send_reaction(account, phone: str, wamid: str, emoji: str) -> None:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "reaction",
        "reaction": {"message_id": wamid, "emoji": emoji or ""},
    }
    _request("POST", _messages_url(account), account.access_token, payload)


def exchange_code_for_token(code: str, app_id: str, app_secret: str, api_version: str) -> str:
    qs = urllib.parse.urlencode(
        {"client_id": app_id, "client_secret": app_secret, "code": code}
    )
    url = f"{BASE_URL}/{api_version or 'v21.0'}/oauth/access_token?{qs}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WhatsAppError(detail or f"token exchange failed ({exc.code})") from exc
    token = raw.get("access_token") or ""
    if not token:
        raise WhatsAppError("no access token in response")
    return token


def debug_token(input_token: str, app_access_token: str) -> dict:
    url = f"{BASE_URL}/debug_token?input_token={urllib.parse.quote(input_token)}"
    raw = _request("GET", url, app_access_token) or {}
    return raw.get("data") or raw


def shared_waba(access_token: str) -> list:
    url = f"{BASE_URL}/me/accounts?fields=id,name,phone_numbers{{id,display_phone_number,verified_name}}"
    raw = _request("GET", url, access_token) or {}
    return list(raw.get("data") or [])


def waba_phone_numbers(waba_id: str, access_token: str, api_version: str = "v21.0") -> list:
    url = f"{BASE_URL}/{api_version}/{waba_id}/phone_numbers?fields=id,display_phone_number,verified_name,quality_rating"
    raw = _request("GET", url, access_token) or {}
    return list(raw.get("data") or [])


def phone_number_info(phone_id: str, access_token: str, api_version: str = "v21.0") -> dict:
    url = f"{BASE_URL}/{api_version}/{phone_id}?fields=verified_name,display_phone_number,quality_rating,is_on_biz_app,platform_type"
    return _request("GET", url, access_token) or {}


def upload_profile_picture(account, data: bytes, mime_type: str) -> str:
    handle = upload_session_media(account, data, mime_type or "image/jpeg", "profile.jpg")
    if not handle:
        raise WhatsAppError("failed to upload profile picture")
    update_business_profile(
        account,
        {"messaging_product": "whatsapp", "profile_picture_handle": handle},
    )
    return handle


def delete_message_template(account, name: str) -> None:
    url = _graph(account, account.business_id) + f"/message_templates?name={urllib.parse.quote(name)}"
    try:
        _request("DELETE", url, account.access_token)
    except WhatsAppError:
        logger.warning("Failed to delete Meta template %s", name)


def submit_message_template(account, template) -> str:
    header_type = (template.header_type or "").upper()
    category = (template.category or "").upper()
    components = []
    if category == "AUTHENTICATION":
        body = {"type": "BODY"}
        if template.add_security_recommendation:
            body["add_security_recommendation"] = True
        components.append(body)
        if template.code_expiration_minutes:
            components.append(
                {"type": "FOOTER", "code_expiration_minutes": template.code_expiration_minutes}
            )
        buttons = []
        for btn in template.buttons or []:
            if not isinstance(btn, dict):
                continue
            item = {"type": (btn.get("type") or "OTP").upper(), "otp_type": btn.get("otp_type") or "COPY_CODE"}
            if btn.get("text"):
                item["text"] = btn["text"]
            buttons.append(item)
        if buttons:
            components.append({"type": "BUTTONS", "buttons": buttons})
    else:
        if header_type and header_type != "NONE":
            header = {"type": "HEADER", "format": header_type}
            if header_type == "TEXT":
                header["text"] = template.header_content or ""
            elif header_type in {"IMAGE", "VIDEO", "DOCUMENT"} and template.header_content:
                header["example"] = {"header_handle": [template.header_content]}
            else:
                header = None
            if header:
                components.append(header)
        body = {"type": "BODY", "text": template.body_content or ""}
        if "{{" in (template.body_content or "") and template.sample_values:
            examples = []
            for item in template.sample_values:
                if isinstance(item, dict) and item.get("value"):
                    examples.append(str(item["value"]))
                elif isinstance(item, str):
                    examples.append(item)
            if examples:
                body["example"] = {"body_text": [examples]}
        components.append(body)
        if template.footer_content:
            components.append({"type": "FOOTER", "text": template.footer_content})
        buttons = []
        for btn in template.buttons or []:
            if not isinstance(btn, dict) or not btn.get("text"):
                continue
            btn_type = (btn.get("type") or "QUICK_REPLY").upper()
            if btn_type == "URL":
                buttons.append({"type": "URL", "text": btn["text"], "url": btn.get("url") or ""})
            elif btn_type == "PHONE_NUMBER":
                buttons.append(
                    {"type": "PHONE_NUMBER", "text": btn["text"], "phone_number": btn.get("phone_number") or ""}
                )
            else:
                buttons.append({"type": "QUICK_REPLY", "text": btn["text"]})
        if buttons:
            components.append({"type": "BUTTONS", "buttons": buttons})

    if template.meta_template_id:
        url = f"{BASE_URL}/{_version(account)}/{template.meta_template_id}"
        _request("POST", url, account.access_token, {"components": components})
        return template.meta_template_id

    url = _graph(account, account.business_id) + "/message_templates"
    payload = {
        "name": template.name,
        "language": template.language,
        "category": category,
        "components": components,
    }
    raw = _request("POST", url, account.access_token, payload) or {}
    meta_id = raw.get("id") or ""
    if not meta_id:
        raise WhatsAppError("Meta did not return a template id")
    return meta_id
