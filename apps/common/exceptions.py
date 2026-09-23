from rest_framework import status


class APIError(Exception):
    def __init__(self, message, status_code=status.HTTP_400_BAD_REQUEST, error_type=""):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


class EntitlementError(APIError):
    def __init__(self, message, status_code=status.HTTP_402_PAYMENT_REQUIRED, error_type="entitlement"):
        super().__init__(message, status_code=status_code, error_type=error_type)



class FeatureEntitlementError(EntitlementError):
    def __init__(
        self,
        message,
        *,
        feature_key,
        current_plan,
        required_plan,
    ):
        super().__init__(
            message,
            status_code=status.HTTP_403_FORBIDDEN,
            error_type="feature_restricted",
        )
        self.feature_key = feature_key
        self.current_plan = current_plan
        self.required_plan = required_plan


class QuotaExceededError(EntitlementError):
    def __init__(
        self,
        message,
        *,
        quota,
        current_plan,
    ):
        super().__init__(
            message,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            error_type="quota_exceeded",
        )
        self.quota = quota
        self.current_plan = current_plan