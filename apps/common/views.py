from django.db import connection
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from .envelope import error, success


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return success({"status": "ok"})


@api_view(["GET"])
@permission_classes([AllowAny])
def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return error("database unavailable", http_status=503)
    return success({"status": "ready"})
