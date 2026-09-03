"""Re-export billing permission used by API views."""

from apps.common.permissions import HasBillingAccess

__all__ = ["HasBillingAccess"]
