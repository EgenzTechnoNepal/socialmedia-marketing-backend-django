from rest_framework import status


class APIError(Exception):
    def __init__(self, message, status_code=status.HTTP_400_BAD_REQUEST, error_type=""):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


class EntitlementError(APIError):
    def __init__(self, message, status_code=status.HTTP_402_PAYMENT_REQUIRED, error_type="entitlement"):
        super().__init__(message, status_code=status_code, error_type=error_type)
