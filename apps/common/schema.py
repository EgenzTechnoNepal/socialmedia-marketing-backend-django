"""Apply the frozen GORM product schema to an empty Postgres database."""

from __future__ import annotations

import logging
from pathlib import Path

from django.db import connection

logger = logging.getLogger(__name__)

# Django migrate owns these. Skip them when applying the GORM dump.
_DJANGO_OWNED_MARKERS = (
    "billing_plans",
    "billing_usage_counters",
    "billing_usage_outbox",
    "organization_subscriptions",
    "dodo_webhook_events",
    "auth_group",
    "auth_permission",
    "auth_user",
    "django_admin_log",
    "django_content_type",
    "django_migrations",
    "django_session",
)

_SKIP_PREFIXES = ("\\",)
_SKIP_SQL_PREFIXES = (
    "set ",
    "select pg_catalog.set_config('search_path'",
)

_IGNORABLE_DDL = (
    "already exists",
    "duplicate",
    "multiple primary keys",
)


def schema_sql_path() -> Path:
    # backend/apps/common/schema.py → backend/sql/product_schema.sql
    return Path(__file__).resolve().parents[2] / "sql" / "product_schema.sql"


def extra_tables_sql_path() -> Path:
    return Path(__file__).resolve().parents[2] / "sql" / "extra_product_tables.sql"


def users_table_exists() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.users')")
        row = cursor.fetchone()
    return bool(row and row[0])


def _clean_dump_statements(raw: str) -> list[str]:
    kept: list[str] = []
    buf: list[str] = []

    def flush():
        stmt = "".join(buf).strip()
        buf.clear()
        if not stmt:
            return
        lower = stmt.lower()
        if any(marker in lower for marker in _DJANGO_OWNED_MARKERS):
            return
        if any(lower.startswith(p) for p in _SKIP_SQL_PREFIXES):
            return
        kept.append(stmt)

    for line in raw.splitlines(True):
        stripped = line.lstrip()
        if stripped.startswith(_SKIP_PREFIXES):
            continue
        buf.append(line)
        if stripped.rstrip().endswith(";"):
            flush()
    flush()
    return kept


def _clean_dump(raw: str) -> str:
    return "\n".join(s if s.endswith(";") else s + ";" for s in _clean_dump_statements(raw))


def _execute_statements(statements: list[str]) -> None:
    """Run DDL one statement at a time so Neon (and poolers) can apply a large dump."""
    if not statements:
        return
    autocommit = connection.get_autocommit()
    connection.set_autocommit(True)
    try:
        with connection.cursor() as cursor:
            for stmt in statements:
                try:
                    cursor.execute(stmt)
                except Exception as exc:
                    text = str(exc).lower()
                    if any(token in text for token in _IGNORABLE_DDL):
                        continue
                    raise RuntimeError(f"Schema statement failed: {exc}") from exc
    finally:
        connection.set_autocommit(autocommit)


def apply_product_schema() -> bool:
    """Create GORM product tables if `users` is missing. Always ensures extra tables.

    Returns True when the main product dump ran.
    """
    applied = False
    if not users_table_exists():
        path = schema_sql_path()
        if not path.is_file():
            raise FileNotFoundError(
                f"Product schema file missing: {path}. Restore backend/sql/product_schema.sql."
            )
        statements = _clean_dump_statements(path.read_text(encoding="utf-8"))
        if not statements:
            raise RuntimeError("Product schema SQL is empty after cleaning")
        _execute_statements(statements)
        logger.info("Applied product schema from %s", path)
        applied = True

    extra = extra_tables_sql_path()
    if extra.is_file():
        _execute_statements(_clean_dump_statements(extra.read_text(encoding="utf-8")))
    return applied
