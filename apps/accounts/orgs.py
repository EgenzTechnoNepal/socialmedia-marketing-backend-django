import logging
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from django.conf import settings as dj_settings
from django.http import FileResponse
from django.utils import timezone as dj_tz
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser

from apps.accounts.models import CustomRole, Organization, User, UserOrganization
from apps.accounts.roles import seed_system_roles
from apps.billing.entitlements import assert_can_add_seat
from apps.common.envelope import error, success
from apps.common.http import is_super, org_id, require_perm, user_iso
from apps.common.permissions import CookieAuthenticated
from apps.common.tokens import redis_client

logger = logging.getLogger(__name__)


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def _org_payload(org) -> dict:
    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "created_at": user_iso(org.created_at),
    }


def _member_payload(m, user) -> dict:
    role_name = ""
    if m.role_id:
        role = CustomRole.objects.filter(id=m.role_id).first()
        role_name = role.name if role else ""
    return {
        "id": str(m.id),
        "user_id": str(m.user_id),
        "organization_id": str(m.organization_id),
        "role_id": str(m.role_id) if m.role_id else None,
        "role_name": role_name,
        "is_default": m.is_default,
        "email": user.email,
        "full_name": user.full_name or "",
        "is_active": user.is_active,
        "created_at": m.created_at,
    }


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def organizations_collection(request):
    if request.method == "GET":
        if not is_super(request):
            require_perm(request, "organizations", "read")
        if is_super(request):
            orgs = Organization.objects.all().order_by("name")
        else:
            ids = UserOrganization.objects.filter(user=request.user).values("organization_id")
            orgs = Organization.objects.filter(id__in=ids).order_by("name")
        return success({"organizations": [_org_payload(o) for o in orgs]})

    require_perm(request, "organizations", "write")
    data = request.data if isinstance(request.data, dict) else {}
    name = (data.get("name") or "").strip()
    if not name:
        return error("Organization name is required", http_status=400)
    org = Organization.objects.create(name=name, slug=_slug(name), settings={})
    admin_role = seed_system_roles(org)
    UserOrganization.objects.create(
        user=request.user, organization=org, role_id=admin_role.id, is_default=False
    )
    return success(_org_payload(org), http_status=201)


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def current_organization(request):
    oid = org_id(request)
    try:
        org = Organization.objects.get(id=oid)
    except Organization.DoesNotExist:
        return error("Organization not found", http_status=404)
    return success(_org_payload(org))


@api_view(["GET", "PUT"])
@permission_classes([CookieAuthenticated])
def org_settings(request):
    oid = org_id(request)
    try:
        org = Organization.objects.get(id=oid)
    except Organization.DoesNotExist:
        return error("Organization not found", http_status=404)
    settings = org.settings or {}
    if request.method == "GET":
        return success(
            {
                "name": org.name,
                "mask_phone_numbers": bool(settings.get("mask_phone_numbers")),
                "timezone": settings.get("timezone") or "UTC",
                "date_format": settings.get("date_format") or "",
                "calling_enabled": bool(settings.get("calling_enabled")),
                "max_call_duration": settings.get("max_call_duration") or 0,
                "transfer_timeout_secs": settings.get("transfer_timeout_secs") or 0,
                "hold_music_file": settings.get("hold_music_file") or "",
                "ringback_file": settings.get("ringback_file") or "",
                "meta_app_id": settings.get("meta_app_id") or "",
                "meta_config_id": settings.get("meta_config_id") or "",
                "has_meta_app_secret": bool(settings.get("meta_app_secret_encrypted")),
            }
        )
    data = request.data if isinstance(request.data, dict) else {}
    if any(k.startswith("meta_") for k in data):
        require_perm(request, "accounts", "write")
    for key in (
        "mask_phone_numbers",
        "timezone",
        "date_format",
        "calling_enabled",
        "max_call_duration",
        "transfer_timeout_secs",
        "hold_music_file",
        "ringback_file",
        "meta_app_id",
        "meta_config_id",
    ):
        if key in data:
            settings[key] = data[key]
    if data.get("meta_app_secret"):
        from django.conf import settings as dj_settings
        from services.crypto import encrypt

        settings["meta_app_secret_encrypted"] = encrypt(data["meta_app_secret"], dj_settings.ENCRYPTION_KEY)
    org.settings = settings
    if "name" in data and data.get("name"):
        org.name = data["name"]
    org.save()
    _invalidate_org_calling_cache(oid)
    return success({"message": "Settings updated"})


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
def members_collection(request):
    oid = org_id(request)
    if request.method == "GET":
        require_perm(request, "organizations", "read")
        members = UserOrganization.objects.filter(organization_id=oid).select_related("user")
        payload = []
        for m in members:
            try:
                user = m.user
            except User.DoesNotExist:
                continue
            payload.append(_member_payload(m, user))
        return success({"members": payload})

    require_perm(request, "organizations", "assign")
    assert_can_add_seat(oid)
    data = request.data if isinstance(request.data, dict) else {}
    user = None
    if data.get("user_id"):
        user = User.objects.filter(id=data["user_id"]).first()
    elif data.get("email"):
        user = User.objects.filter(email=data["email"].strip()).first()
    if not user:
        return error("User not found", http_status=404)
    if UserOrganization.objects.filter(user=user, organization_id=oid).exists():
        return error("You are already a member of this organization", http_status=409)
    role_id = data.get("role_id")
    if not role_id:
        role = CustomRole.objects.filter(organization_id=oid, is_default=True).first()
        role_id = role.id if role else None
    m = UserOrganization.objects.create(
        user=user, organization_id=oid, role_id=role_id, is_default=False
    )
    return success(_member_payload(m, user), http_status=201)


@api_view(["PUT", "DELETE"])
@permission_classes([CookieAuthenticated])
def member_detail(request, member_id):
    oid = org_id(request)
    require_perm(request, "organizations", "assign")
    try:
        m = UserOrganization.objects.get(id=member_id, organization_id=oid)
    except UserOrganization.DoesNotExist:
        return error("Member not found", http_status=404)
    if request.method == "DELETE":
        m.deleted_at = dj_tz.now()
        m.save(update_fields=["deleted_at"])
        return success({"message": "Member removed"})
    data = request.data if isinstance(request.data, dict) else {}
    if "role_id" in data:
        m.role_id = data.get("role_id")
        m.save(update_fields=["role_id", "updated_at"])
    return success(_member_payload(m, m.user))


@api_view(["GET", "POST"])
@permission_classes([CookieAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def org_audio(request):
    oid = org_id(request)
    audio_type = request.query_params.get("type") or ""
    if audio_type not in {"hold_music", "ringback"}:
        return error("Query parameter 'type' must be 'hold_music' or 'ringback'", http_status=400)
    if request.method == "GET":
        return _serve_org_audio(oid, audio_type)
    require_perm(request, "organizations", "write")
    upload = request.FILES.get("file")
    if not upload:
        return error("No file provided", http_status=400)
    data = upload.read()
    if len(data) > 5 * 1024 * 1024:
        return error("File too large. Maximum size is 5MB", http_status=400)

    filename = f"org_{oid}_{audio_type}.ogg"
    dest = _calling_audio_dir() / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    _transcode_to_opus(data, dest)

    org = Organization.objects.get(id=oid)
    org_settings = org.settings or {}
    field = "hold_music_file" if audio_type == "hold_music" else "ringback_file"
    org_settings[field] = filename
    org.settings = org_settings
    org.save(update_fields=["settings", "updated_at"])
    _invalidate_org_calling_cache(oid)
    return success({"filename": filename, "type": audio_type})


def _calling_audio_dir() -> Path:
    return Path(dj_settings.CALLING_AUDIO_DIR)


def _invalidate_org_calling_cache(oid) -> None:
    try:
        redis_client().delete(f"org:calling_settings:{oid}")
    except Exception:
        logger.exception("Failed to invalidate org calling settings cache")


def _transcode_to_opus(data: bytes, dest: Path) -> None:
    """Match Go transcodeToOpus (libopus 48kHz mono). Fall back to the raw bytes if ffmpeg is missing."""
    fd, tmp_path = tempfile.mkstemp(prefix="org-audio-")
    try:
        os.write(fd, data)
        os.close(fd)
        fd = -1
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                tmp_path,
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "libopus",
                "-b:a",
                "48k",
                "-application",
                "audio",
                "-frame_duration",
                "20",
                "-vn",
                str(dest),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode == 0 and dest.is_file():
            return
        logger.warning("ffmpeg transcode failed; storing original bytes: %s", result.stderr[-500:] if result.stderr else "")
        dest.write_bytes(data)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffmpeg unavailable for org audio (%s); storing original bytes", exc)
        dest.write_bytes(data)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _serve_org_audio(oid, audio_type: str):
    try:
        org = Organization.objects.get(id=oid)
    except Organization.DoesNotExist:
        return error("Organization not found", http_status=404)
    field = "hold_music_file" if audio_type == "hold_music" else "ringback_file"
    stored = (org.settings or {}).get(field) or ""
    path = _resolve_org_audio_path(oid, audio_type, stored)
    if path is None:
        return error("Audio file not found", http_status=404)
    return FileResponse(open(path, "rb"), content_type="audio/ogg")


def _resolve_org_audio_path(oid, audio_type: str, stored_name: str) -> Path | None:
    audio_dir = _calling_audio_dir().resolve()
    canonical = audio_dir / f"org_{oid}_{audio_type}.ogg"
    if canonical.is_file():
        return canonical
    candidates = []
    if stored_name:
        normalized = stored_name.replace("\\", "/").lstrip("/")
        candidates.append(audio_dir / Path(normalized).name)
        candidates.append(Path(dj_settings.STORAGE_LOCAL_PATH) / normalized)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        if audio_dir in resolved.parents or resolved.parent == audio_dir:
            return resolved
        storage_root = Path(dj_settings.STORAGE_LOCAL_PATH).resolve()
        if storage_root in resolved.parents or resolved.parent == storage_root:
            return resolved
    return None
