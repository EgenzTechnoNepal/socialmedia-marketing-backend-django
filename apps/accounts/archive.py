from rest_framework.decorators import api_view, permission_classes

from apps.common.envelope import success
from apps.common.permissions import CookieAuthenticated


@api_view(["GET"])
@permission_classes([CookieAuthenticated])
def archive_status(request):
    return success(
        {
            "available": False,
            "status": "coming_soon",
            "message": "Archive feature coming soon",
        }
    )
