"""
Authentication and session security tests.

Run with:
    python manage.py test apps.accounts.tests.test_auth_security
"""
from datetime import datetime, timedelta, timezone

import jwt

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import bcrypt
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from apps.common.schema import apply_product_schema

from apps.accounts.models import Organization, User, UserOrganization


def make_org():
    suffix = uuid4().hex[:8]
    return Organization.objects.create(
        name=f"Auth Test Org {suffix}",
        slug=f"auth-test-org-{suffix}",
    )

def setUpModule():
    apply_product_schema()


@override_settings(
    JWT_ACCESS_EXPIRY_MINS=15,
    JWT_REFRESH_EXPIRY_DAYS=1,
)
class AuthenticationSecurityTests(TestCase):
    def setUp(self):
        self.org = make_org()

        self.email = f"auth-test-{uuid4().hex[:8]}@example.com"
        self.password = "TestPassword123!"

        self.user = User.objects.create(
            organization=self.org,
            email=self.email,
            password_hash=bcrypt.hashpw(
                self.password.encode("utf-8"),
                bcrypt.gensalt(rounds=10),
            ).decode("utf-8"),
            full_name="Auth Test User",
            is_active=True,
            is_super_admin=False,
        )

        UserOrganization.objects.create(
            user=self.user,
            organization=self.org,
        )

        self.client = APIClient()

    def test_login_sets_auth_cookies(self):
        from django.conf import settings
        response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)

        self.assertIn("whm_access", response.cookies)
        self.assertIn("whm_refresh", response.cookies)
        self.assertIn("whm_csrf", response.cookies)

        access_cookie = response.cookies["whm_access"]
        refresh_cookie = response.cookies["whm_refresh"]
        csrf_cookie = response.cookies["whm_csrf"]

        self.assertTrue(access_cookie["httponly"])
        self.assertTrue(refresh_cookie["httponly"])
        self.assertFalse(csrf_cookie["httponly"])

        # Secure
        expected_secure = settings.COOKIE_SECURE
        self.assertEqual(bool(access_cookie["secure"]), expected_secure)
        self.assertEqual(bool(refresh_cookie["secure"]), expected_secure)
        self.assertEqual(bool(csrf_cookie["secure"]), expected_secure)
        # SameSite
        self.assertEqual(access_cookie["samesite"].lower(), "lax")
        self.assertEqual(refresh_cookie["samesite"].lower(), "lax")
        self.assertEqual(csrf_cookie["samesite"].lower(), "lax")

        # Paths
        self.assertEqual(access_cookie["path"], "/api")
        self.assertEqual(refresh_cookie["path"], "/api/auth/refresh")
        self.assertEqual(csrf_cookie["path"], "/")

        # Max-Age
        self.assertEqual(
            int(access_cookie["max-age"]),
            settings.JWT_ACCESS_EXPIRY_MINS * 60,
        )
        self.assertEqual(
            int(refresh_cookie["max-age"]),
            settings.JWT_REFRESH_EXPIRY_DAYS * 86400,
        )
        self.assertEqual(
            int(csrf_cookie["max-age"]),
            settings.JWT_REFRESH_EXPIRY_DAYS * 86400,
        )

    def test_login_rejects_invalid_password(self):
        response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": "WrongPassword123!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 401)

    def test_refresh_rotates_refresh_token(self):
        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        original_refresh = login_response.cookies["whm_refresh"].value

        self.client.cookies["whm_refresh"] = original_refresh

        refresh_response = self.client.post(
            "/api/auth/refresh",
            {},
            format="json",
        )

        self.assertEqual(refresh_response.status_code, 200)

        new_refresh = refresh_response.cookies["whm_refresh"].value

        self.assertNotEqual(original_refresh, new_refresh)

    def test_refresh_token_cannot_be_reused(self):
        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        original_refresh = login_response.cookies["whm_refresh"].value
        # First use of the refresh token should succeed
        self.client.cookies["whm_refresh"] = original_refresh

        first_refresh = self.client.post(
            "/api/auth/refresh",
            {},
            format="json",
        )

        self.assertEqual(first_refresh.status_code, 200)

        # The same refresh token has already been consumed.
        self.client.cookies["whm_refresh"] = original_refresh

        second_refresh = self.client.post(
            "/api/auth/refresh",
            {},
            format="json",
        )

        self.assertEqual(second_refresh.status_code, 401)

    def test_logout_revokes_refresh_token(self):
        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        refresh_token = login_response.cookies["whm_refresh"].value
        csrf_token = login_response.cookies["whm_csrf"].value

        self.client.cookies["whm_refresh"] = refresh_token
        self.client.cookies["whm_csrf"] = csrf_token

        logout_response = self.client.post(
            "/api/auth/logout",
            {},
            format="json",
            HTTP_X_CSRF_TOKEN=csrf_token,
        )

        self.assertEqual(logout_response.status_code, 200)

        self.client.cookies["whm_refresh"] = refresh_token

        refresh_response = self.client.post(
            "/api/auth/refresh",
            {},
            format="json",
        )

        self.assertEqual(refresh_response.status_code, 401)

    def test_concurrent_refresh_token_consumption_allows_only_one(self):
        from apps.common.tokens import (
            consume_refresh_jti,
            redis_client,
            refresh_token_key,
        )

        jti = f"concurrency-test-{uuid4().hex}"
        key = refresh_token_key(jti)

        redis_client().set(key, str(self.user.id), ex=60)

        def consume():
            return consume_refresh_jti(jti)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(consume),
                    executor.submit(consume),
                ]

                results = [future.result() for future in futures]

            self.assertEqual(results.count(True), 1)
            self.assertEqual(results.count(False), 1)

        finally:
            redis_client().delete(key)

    def test_protected_endpoint_requires_authentication(self):
        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 401)

    def test_tampered_access_token_is_rejected(self):
        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        access_token = login_response.cookies["whm_access"].value

        parts = access_token.split(".")
        self.assertEqual(len(parts), 3)

        # Change the signature so the token is no longer valid.
        tampered_token = ".".join(parts[:2] + ["invalid-signature"])

        self.client.cookies["whm_access"] = tampered_token

        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 401)


    def test_expired_access_token_is_rejected(self):
        from django.conf import settings

        now = datetime.now(timezone.utc)

        expired_token = jwt.encode(
            {
                "user_id": str(self.user.id),
                "organization_id": str(self.org.id),
                "email": self.user.email,
                "is_super_admin": False,
                "iss": settings.JWT_ISSUER,
                "iat": now - timedelta(minutes=30),
                "exp": now - timedelta(minutes=1),
            },
            settings.JWT_SECRET,
            algorithm="HS256",
        )

        self.client.cookies["whm_access"] = expired_token

        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 401)

    def test_inactive_user_access_token_is_rejected(self):
        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        access_token = login_response.cookies["whm_access"].value

        # Disable the user after the token has already been issued.
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        self.client.cookies["whm_access"] = access_token

        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 401)


    def test_logout_rejects_missing_csrf_token(self):
        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        refresh_token = login_response.cookies["whm_refresh"].value

        self.client.cookies["whm_refresh"] = refresh_token

        response = self.client.post(
            "/api/auth/logout",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 403)


    def test_logout_rejects_invalid_csrf_token(self):
        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        refresh_token = login_response.cookies["whm_refresh"].value

        self.client.cookies["whm_refresh"] = refresh_token

        response = self.client.post(
            "/api/auth/logout",
            {},
            format="json",
            HTTP_X_CSRF_TOKEN="invalid-csrf-token",
        )

        self.assertEqual(response.status_code, 403)


    def test_logout_clears_auth_cookies(self):
        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        csrf_token = login_response.cookies["whm_csrf"].value

        logout_response = self.client.post(
            "/api/auth/logout",
            {},
            format="json",
            HTTP_X_CSRF_TOKEN=csrf_token,
        )

        self.assertEqual(logout_response.status_code, 200)

        self.assertIn("whm_access", logout_response.cookies)
        self.assertIn("whm_refresh", logout_response.cookies)
        self.assertIn("whm_csrf", logout_response.cookies)

        access_cookie = logout_response.cookies["whm_access"]
        refresh_cookie = logout_response.cookies["whm_refresh"]
        csrf_cookie = logout_response.cookies["whm_csrf"]

        self.assertEqual(int(access_cookie["max-age"]), 0)
        self.assertEqual(int(refresh_cookie["max-age"]), 0)
        self.assertEqual(int(csrf_cookie["max-age"]), 0)

        self.assertEqual(access_cookie["path"], "/api")
        self.assertEqual(refresh_cookie["path"], "/api/auth/refresh")
        self.assertEqual(csrf_cookie["path"], "/")

    def test_expired_refresh_token_is_rejected(self):
        from django.conf import settings

        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        now = datetime.now(timezone.utc)

        expired_refresh_token = jwt.encode(
            {
                "user_id": str(self.user.id),
                "organization_id": str(self.org.id),
                "email": self.user.email,
                "is_super_admin": False,
                "iss": settings.JWT_ISSUER,
                "iat": now - timedelta(days=2),
                "exp": now - timedelta(minutes=1),
                "token_type": "refresh",
                "jti": str(uuid4()),
            },
            settings.JWT_SECRET,
            algorithm="HS256",
        )

        self.client.cookies["whm_refresh"] = expired_refresh_token

        response = self.client.post(
            "/api/auth/refresh",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 401)

    def test_password_change_invalidates_existing_refresh_token(self):
        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        refresh_token = login_response.cookies["whm_refresh"].value
        csrf_token = login_response.cookies["whm_csrf"].value

        self.client.cookies["whm_refresh"] = refresh_token
        self.client.cookies["whm_csrf"] = csrf_token

        password_response = self.client.put(
            f"/api/users/{self.user.id}",
            {
                "password": "NewPassword123!",
            },
            format="json",
            HTTP_X_CSRF_TOKEN=csrf_token,
        )

        self.assertEqual(password_response.status_code, 200)

        # The old refresh token should no longer be usable.
        self.client.cookies["whm_refresh"] = refresh_token

        refresh_response = self.client.post(
            "/api/auth/refresh",
            {},
            format="json",
        )

        self.assertEqual(refresh_response.status_code, 401)

    def test_refresh_rejects_old_session_version(self):
        from apps.common.tokens import redis_client, session_version_key

        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        refresh_token = login_response.cookies["whm_refresh"].value
        self.client.cookies["whm_refresh"] = refresh_token

        session_key = session_version_key(self.user.id)

        # Invalidate the existing session by increasing the session version.
        redis_client().incr(session_key)

        response = self.client.post(
            "/api/auth/refresh",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 401)


    @patch("apps.common.tokens.redis_client")
    def test_refresh_returns_503_when_session_version_storage_fails(self, mock_redis_client):
        import redis

        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        refresh_token = login_response.cookies["whm_refresh"].value
        self.client.cookies["whm_refresh"] = refresh_token

        mock_redis = mock_redis_client.return_value
        # Allow the refresh token to be consumed.
        mock_redis.delete.return_value = 1
        mock_redis.get.side_effect = redis.RedisError("Redis unavailable")

        response = self.client.post(
            "/api/auth/refresh",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 503)

    @patch("apps.common.tokens.redis_client")
    def test_login_returns_503_when_session_version_storage_fails(self, mock_redis_client):
        import redis

        mock_redis = mock_redis_client.return_value
        mock_redis.get.side_effect = redis.RedisError("Redis unavailable")

        response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 503)

    @patch("apps.common.tokens.redis_client")
    def test_password_change_returns_503_when_session_invalidation_fails(
        self,
        mock_redis_client,
    ):
        import redis

        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        csrf_token = login_response.cookies["whm_csrf"].value
        self.client.cookies["whm_csrf"] = csrf_token

        mock_redis = mock_redis_client.return_value
        mock_redis.incr.side_effect = redis.RedisError("Redis unavailable")

        response = self.client.put(
            f"/api/users/{self.user.id}",
            {
                "password": "NewPassword123!",
            },
            format="json",
            HTTP_X_CSRF_TOKEN=csrf_token,
        )

        self.assertEqual(response.status_code, 503)


    @patch("apps.common.tokens.redis_client")
    def test_login_returns_503_when_refresh_token_storage_fails(
        self,
        mock_redis_client,
    ):
        import redis

        mock_redis = mock_redis_client.return_value

        # Session version lookup succeeds.
        mock_redis.get.return_value = "1"

        # Refresh token cannot be stored.
        mock_redis.set.side_effect = redis.RedisError("Redis unavailable")

        response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 503)


    @patch("apps.common.tokens.redis_client")
    def test_refresh_returns_503_when_refresh_token_consumption_fails(
        self,
        mock_redis_client,
    ):
        import redis

        login_response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)

        refresh_token = login_response.cookies["whm_refresh"].value
        self.client.cookies["whm_refresh"] = refresh_token

        mock_redis = mock_redis_client.return_value

        # Redis fails while consuming the existing refresh token.
        mock_redis.delete.side_effect = redis.RedisError("Redis unavailable")

        response = self.client.post(
            "/api/auth/refresh",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 503)


    def test_login_rate_limit_blocks_excessive_attempts(self):
        for _ in range(5):
            response = self.client.post(
                "/api/auth/login",
                {
                    "email": self.email,
                    "password": "WrongPassword123!",
                },
                format="json",
            )
            self.assertEqual(response.status_code, 401)

        response = self.client.post(
            "/api/auth/login",
            {
                "email": self.email,
                "password": "WrongPassword123!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 429)
