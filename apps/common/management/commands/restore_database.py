"""Restore a pg_dump custom-format file onto the current DATABASE_URL."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def _backend_dir() -> Path:
    return Path(__file__).resolve().parents[4]


class Command(BaseCommand):
    help = "Restore schema + rows from a dump created by dump_database onto the current DATABASE_URL."

    def add_arguments(self, parser):
        parser.add_argument("dump_path", nargs="?", default="backups/whatomate.dump")

    def handle(self, *args, **options):
        pg_restore = shutil.which("pg_restore")
        if not pg_restore:
            raise CommandError(
                "pg_restore was not found on PATH. Install PostgreSQL client tools, then retry."
            )
        dump = Path(options["dump_path"])
        if not dump.is_absolute():
            dump = _backend_dir() / dump
        if not dump.is_file():
            raise CommandError(f"Dump file not found: {dump}")

        db = settings.DATABASES["default"]
        env = os.environ.copy()
        env["PGPASSWORD"] = str(db.get("PASSWORD") or "")
        sslmode = (db.get("OPTIONS") or {}).get("sslmode")
        if sslmode:
            env["PGSSLMODE"] = sslmode

        cmd = [
            pg_restore,
            "-h",
            str(db["HOST"]),
            "-p",
            str(db["PORT"]),
            "-U",
            str(db["USER"]),
            "-d",
            str(db["NAME"]),
            "--no-owner",
            "--no-acl",
            "--clean",
            "--if-exists",
            str(dump),
        ]
        self.stdout.write(f"Restoring {dump.name} → {db['NAME']}@{db['HOST']}")
        try:
            subprocess.run(cmd, check=True, env=env)
        except subprocess.CalledProcessError as exc:
            raise CommandError(f"pg_restore failed with exit {exc.returncode}") from exc
        self.stdout.write(self.style.SUCCESS("Restore complete"))
