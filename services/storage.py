from pathlib import Path

from django.conf import settings


def media_root() -> Path:
    path = Path(settings.STORAGE_LOCAL_PATH)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_bytes(relative_path: str, data: bytes) -> str:
    dest = media_root() / relative_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return relative_path.replace("\\", "/")


def absolute_path(relative_path: str) -> Path:
    return media_root() / relative_path
