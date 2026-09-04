"""Dump the current Postgres (schema + rows) so it can be restored on another host."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def _backend_dir() -> Path:
    # backend/apps/common/management/commands/this.py → backend/
    return Path(__file__).resolve().parents[4]


def _resolve_output(path: str) -> Path:
    out = Path(path)
    if not out.is_absolute():
        out = _backend_dir() / out
    return out


class Command(BaseCommand):
    help = (
        "pg_dump the database Django is using (schema + data). Restore onto a future "
        "Postgres with: python manage.py restore_database backups/whatomate.dump"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "-o",
            "--output",
            default="backups/whatomate.dump",
            help="Output path (custom format, for pg_restore).",
        )

    def handle(self, *args, **options):
        pg_dump = shutil.which("pg_dump")
        if not pg_dump:
            raise CommandError(
                "pg_dump was not found on PATH. Install PostgreSQL client tools, then retry."
            )
        db = settings.DATABASES["default"]
        out = _resolve_output(options["output"])
        out.parent.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["PGPASSWORD"] = str(db.get("PASSWORD") or "")
        sslmode = (db.get("OPTIONS") or {}).get("sslmode")
        if sslmode:
            env["PGSSLMODE"] = sslmode

        cmd = [
            pg_dump,
            "-h",
            str(db["HOST"]),
            "-p",
            str(db["PORT"]),
            "-U",
            str(db["USER"]),
            "-d",
            str(db["NAME"]),
            "-Fc",
            "--no-owner",
            "--no-acl",
            "-f",
            str(out),
        ]
        self.stdout.write(f"Dumping {db['NAME']}@{db['HOST']} → {out}")
        try:
            subprocess.run(cmd, check=True, env=env)
        except subprocess.CalledProcessError as exc:
            raise CommandError(f"pg_dump failed with exit {exc.returncode}") from exc
        self.stdout.write(self.style.SUCCESS(f"Wrote {out}"))
        self.stdout.write(
            "To move to another Postgres: set DATABASE_URL, then "
            "python manage.py restore_database backups/whatomate.dump"
        )
