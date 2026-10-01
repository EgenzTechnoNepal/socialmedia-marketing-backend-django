import logging
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import redis
from django.conf import settings

from apps.accounts.models import User

logger = logging.getLogger(__name__)

_redis = None

# Valid bcrypt of a dummy string — used only to keep login timing even when the email is unknown.
DUMMY_BCRYPT = b"$2b$10$5dc6chKPANAoKXxMi3XZ0./doPimFFmBiAHu9Rd4e/5Q/1HwchfBm"

class RefreshTokenStorageError(Exception):
    """Raised when a refresh token cannot be persisted in Redis."""


class RefreshTokenConsumptionError(Exception):
    """Raised when a refresh token cannot be consumed in Redis."""

class SessionInvalidationError(Exception):
    """Raised when user session invalidation cannot be persisted in Redis."""

class SessionVersionStorageError(Exception):
    """Raised when the session version cannot be read from Redis."""


def redis_client():
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def refresh_token_key(jti: str) -> str:
    return f"refresh:{jti}"


def session_version_key(user_id) -> str:
    return f"auth:session-version:{user_id}"


def get_session_version(user_id) -> int:
    try:
        value = redis_client().get(session_version_key(user_id))

        if value is None:
            stored = redis_client().set(session_version_key(user_id), "1")
            if not stored:
                raise SessionVersionStorageError(
                    "Unable to initialize session version"
                )
            return 1

        return int(value)

    except redis.RedisError as exc:
        logger.exception("Failed to get session version from Redis")
        raise SessionVersionStorageError(
            "Unable to read session version"
        ) from exc

    except ValueError as exc:
        logger.exception("Invalid session version stored in Redis")
        raise SessionVersionStorageError(
            "Invalid session version"
        ) from exc

def invalidate_user_sessions(user_id) -> None:
    try:
        redis_client().incr(session_version_key(user_id))
    except redis.RedisError as exc:
        logger.exception("Failed to invalidate user sessions in Redis")
        raise SessionInvalidationError(
            "Unable to invalidate user sessions"
        ) from exc


def _now():
    return datetime.now(timezone.utc)


def _base_claims(user: User, organization_id) -> dict:
    claims = {
        "user_id": str(user.id),
        "organization_id": str(organization_id),
        "email": user.email,
        "is_super_admin": bool(user.is_super_admin),
        "iss": settings.JWT_ISSUER,
        "iat": _now(),
    }
    if user.role_id:
        claims["role_id"] = str(user.role_id)
    return claims


def generate_access_token(user: User, organization_id=None) -> str:
    org_id = organization_id if organization_id is not None else user.organization_id
    claims = _base_claims(user, org_id)
    claims["exp"] = _now() + timedelta(minutes=settings.JWT_ACCESS_EXPIRY_MINS)
    return jwt.encode(claims, settings.JWT_SECRET, algorithm="HS256")


def generate_refresh_token(user: User, organization_id=None) -> str:
    org_id = organization_id if organization_id is not None else user.organization_id
    jti = str(uuid.uuid4())
    expiry = timedelta(days=settings.JWT_REFRESH_EXPIRY_DAYS)

    claims = _base_claims(user, org_id)
    claims["jti"] = jti
    claims["session_version"] = get_session_version(user.id)
    claims["exp"] = _now() + expiry

    signed = jwt.encode(claims, settings.JWT_SECRET, algorithm="HS256")
    try:
        stored = redis_client().set(
            refresh_token_key(jti),
            str(user.id),
            ex=int(expiry.total_seconds()),
        )
    except redis.RedisError as exc:
        logger.exception("Failed to store refresh token in Redis")
        raise RefreshTokenStorageError(
            "Unable to create a persistent refresh session"
        ) from exc

    if not stored:
        logger.error("Redis did not confirm refresh token storage")
        raise RefreshTokenStorageError(
            "Unable to create a persistent refresh session"
        )

    return signed


def generate_ws_token(user_id, organization_id) -> str:
    claims = {
        "user_id": str(user_id),
        "organization_id": str(organization_id),
        "iss": settings.JWT_ISSUER,
        "sub": "ws",
        "iat": _now(),
        "exp": _now() + timedelta(seconds=30),
    }
    return jwt.encode(claims, settings.JWT_SECRET, algorithm="HS256")


def decode_token(token: str, *, verify_exp: bool = True) -> dict:
    return jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=["HS256"],
        issuer=settings.JWT_ISSUER,
        options={"require": ["exp"], "verify_exp": verify_exp},
    )


def consume_refresh_jti(jti: str) -> bool:
    if not jti:
        return True
    try:
        deleted = redis_client().delete(refresh_token_key(jti))
    except redis.RedisError as exc:
        raise RefreshTokenConsumptionError(
            "Unable to validate the refresh session"
        ) from exc
    return deleted == 1


def revoke_refresh_jti(jti: str) -> None:
    if not jti:
        return
    try:
        redis_client().delete(refresh_token_key(jti))
    except redis.RedisError:
        logger.exception("Failed to revoke refresh token in Redis")
