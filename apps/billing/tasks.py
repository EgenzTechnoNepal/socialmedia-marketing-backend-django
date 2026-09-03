import logging

from celery import shared_task
from django.db.models import F
from django.utils import timezone

from .models import OrganizationSubscription, UsageEventOutbox
from .services import dodo
from .services.dodo import DodoNotConfigured

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=5, default_retry_delay=30)
def ingest_usage_events(self):
    pending = list(
        UsageEventOutbox.objects.filter(ingested_at__isnull=True).order_by("created_at")[:1000]
    )
    if not pending:
        return {"ingested": 0}
    if not dodo.is_configured():
        return {"ingested": 0, "skipped": "dodo_not_configured"}

    events = []
    rows_to_send = []
    for row in pending:
        sub = OrganizationSubscription.objects.filter(organization_id=row.organization_id).first()
        customer_id = sub.dodo_customer_id if sub else ""
        if not customer_id:
            continue
        rows_to_send.append(row)
        events.append(
            {
                "event_id": row.event_id,
                "customer_id": customer_id,
                "event_name": row.event_name,
                "metadata": {**(row.metadata or {}), "quantity": row.quantity},
            }
        )
    if not events:
        return {"ingested": 0, "skipped": "no_customer"}

    try:
        dodo.ingest_events(events)
    except DodoNotConfigured:
        return {"ingested": 0, "skipped": "dodo_not_configured"}
    except Exception as exc:
        UsageEventOutbox.objects.filter(id__in=[row.id for row in rows_to_send]).update(
            attempts=F("attempts") + 1,
            last_error=str(exc)[:2000],
        )
        logger.exception("Usage ingest failed")
        raise self.retry(exc=exc)

    now = timezone.now()
    UsageEventOutbox.objects.filter(id__in=[row.id for row in rows_to_send]).update(
        ingested_at=now, last_error=""
    )
    return {"ingested": len(events)}
