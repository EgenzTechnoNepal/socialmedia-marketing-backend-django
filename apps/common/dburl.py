"""Parse DATABASE_URL for Django (Neon, local Docker, or any future Postgres)."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse


def neon_direct_host(host: str) -> str:
    """Neon pooler hostnames break DDL. Use the compute endpoint for migrate/seed."""
    if host and "-pooler." in host:
        return host.replace("-pooler.", ".", 1)
    return host


def parse_database_url(url: str) -> dict:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must start with postgresql://")
    name = unquote((parsed.path or "/").lstrip("/"))
    if not name:
        name = "neondb"
    name = name.split("?")[0]
    qs = parse_qs(parsed.query)
    sslmode = (qs.get("sslmode") or ["require"])[0]
    host = parsed.hostname or "127.0.0.1"
    pooled = "-pooler." in host
    options = {"sslmode": sslmode}
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": name,
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": host,
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": 0 if pooled else 60,
        "DISABLE_SERVER_SIDE_CURSORS": pooled,
        "OPTIONS": options,
    }


def with_direct_host(config: dict) -> dict:
    out = dict(config)
    host = neon_direct_host(str(config.get("HOST") or ""))
    out["HOST"] = host
    out["CONN_MAX_AGE"] = 0
    out["DISABLE_SERVER_SIDE_CURSORS"] = False
    options = dict(config.get("OPTIONS") or {})
    if options.get("sslmode"):
        out["OPTIONS"] = options
    return out
