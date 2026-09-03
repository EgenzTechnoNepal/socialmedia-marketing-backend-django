import re

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\s*\}\}")


def nested_get(data: dict | None, path: str):
    if not data or not path:
        return None
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def process_template(template: str, data: dict | None) -> str:
    if not template:
        return ""
    data = data or {}

    def repl(match):
        value = nested_get(data, match.group(1))
        if value is None:
            return ""
        return str(value)

    return _VAR.sub(repl, template)


def extract_mapping(payload: dict, mapping: dict) -> dict:
    out = {}
    for name, path in (mapping or {}).items():
        if isinstance(path, str) and name:
            value = nested_get(payload, path)
            if value is not None:
                out[name] = value
    return out
