"""
Service layer for processing events - replaces management commands
"""

from django.utils import timezone
from datetime import timedelta
from typing import List
from .models import Event, EventStatus
import logging

logger = logging.getLogger(__name__)


class EventProcessor:
    """Service for processing due events"""

    def process_due_events(self, lookback_hours: int = 24) -> dict:
        since = timezone.now() - timedelta(hours=lookback_hours)
        now = timezone.now()

        # Get events efficiently using the optimized query
        due_events = Event.objects.get_events(
            from_date=since,
            to_date=now,
            status=EventStatus.PENDING,
            include_immediate=True,
        )

        occurred_count = 0
        for event in due_events:
            try:
                event.mark_as_occurred()
                occurred_count += 1
            except Exception as e:
                logger.error(f"Failed to process event {event.id}: {e}")

        return {
            "due_events_found": len(due_events),
            "events_processed": occurred_count,
            "timestamp": now.isoformat(),
        }

    def cleanup_old_events(self, days_old: int = 30) -> dict:
        """Remove old completed/cancelled events"""
        cutoff = timezone.now() - timedelta(days=days_old)

        old_events = Event.objects.filter(
            status__in=[EventStatus.PROCESSED, EventStatus.CANCELLED],
            event_updated_at__lt=cutoff,
        )

        count = old_events.count()
        old_events.delete()

        return {"deleted_count": count, "cutoff_date": cutoff.isoformat()}


# Global processor instance
event_processor = EventProcessor()
