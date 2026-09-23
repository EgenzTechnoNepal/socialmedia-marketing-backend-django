from django.db import DatabaseError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .exceptions import (
    APIError,
    EntitlementError,
    FeatureEntitlementError,
    QuotaExceededError,
)

def success(data=None, http_status=status.HTTP_200_OK):
    return Response({"status": "success", "data": data if data is not None else {}}, status=http_status)


def error(message, http_status=status.HTTP_400_BAD_REQUEST, error_type="", extra=None):
    payload = {"status": "error", "message": message, "error_type": error_type}
    if extra is not None:
        payload["data"] = extra
    return Response(payload, status=http_status)


def exception_handler(exc, context):
    if isinstance(exc, FeatureEntitlementError):
        return error(
           str(exc),
           http_status=exc.status_code,
           error_type=exc.error_type,
           extra={
              "feature_key": exc.feature_key,
               "current_plan": exc.current_plan,
               "required_plan": exc.required_plan,
            },
        )

    if isinstance(exc, QuotaExceededError):
        return error(
            str(exc),
            http_status=exc.status_code,
            error_type=exc.error_type,
            extra={
               "quota": exc.quota,
               "current_plan": exc.current_plan,
            },
        )

    if isinstance(exc, EntitlementError):
        return error(
            str(exc),
            http_status=exc.status_code,
            error_type=exc.error_type,
        )
    if isinstance(exc, APIError):
        return error(str(exc), http_status=exc.status_code, error_type=exc.error_type)

    if isinstance(exc, DatabaseError):
        return error(
            "Database tables are missing. From the Django folder run: python manage.py migrate && python manage.py seed_admin",
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            error_type="database",
        )

    response = drf_exception_handler(exc, context)
    if response is None:
        return error("Internal server error", http_status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    detail = response.data
    if isinstance(detail, dict) and "detail" in detail:
        message = str(detail["detail"])
    else:
        message = str(detail)
    return error(message, http_status=response.status_code)
