from rest_framework.throttling import SimpleRateThrottle


class LoginRateThrottle(SimpleRateThrottle):
    scope = "login"

    def get_cache_key(self, request, view):
        email = (request.data.get("email") or "").strip().lower()

        if not email:
            return self.get_ident(request)

        return f"login:{self.get_ident(request)}:{email}"