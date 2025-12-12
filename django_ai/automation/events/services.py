"""
Service layer for processing events - replaces management commands
"""

from django.utils import timezone
from django.db import transaction
from datetime import timedelta
from typing import List, Set
from .models import Event, EventStatus, ProcessedEventOffset
from .callbacks import callback_registry, EventPhase
import logging

logger = logging.getLogger(__name__)


class EventProcessor:
    """Service for processing due events"""

    def process_due_events(self, lookback_hours: int = 24) -> dict:
        """Process all due events across all registered offsets.

        For each registered offset, finds events where:
        - event.at + offset <= now
        - event hasn't been processed for this offset yet (or event moved)

        Returns:
            Dict with processing statistics
        """
        since = timezone.now() - timedelta(hours=lookback_hours)
        now = timezone.now()

        # Get all registered offsets for OCCURRED phase
        offsets = callback_registry.get_registered_offsets(EventPhase.OCCURRED)

        # Always include zero offset (immediate/at-time callbacks)
        offsets.add(timedelta())

        total_processed = 0
        offset_results = {}

        for offset in offsets:
            result = self._process_events_for_offset(since, now, offset)
            offset_results[str(offset)] = result
            total_processed += result["processed"]

        return {
            "total_processed": total_processed,
            "offsets_checked": len(offsets),
            "offset_details": offset_results,
            "timestamp": now.isoformat(),
        }

    def _process_events_for_offset(
        self, since: timezone.datetime, now: timezone.datetime, offset: timedelta
    ) -> dict:
        """Process events for a specific offset.

        For zero offset: marks events as OCCURRED (triggers callbacks via Event.save)
        For non-zero offsets: fires callbacks directly without changing event status
        """
        # Adjust the query time range for the offset
        # If offset is -1 hour, we want events where at - 1hr <= now
        # So we query for events where at <= now + 1hr (now - offset when offset is negative)
        adjusted_to = now - offset
        adjusted_since = since - offset

        # For zero offset, only query PENDING events (to mark as OCCURRED)
        # For non-zero offsets, also include PROCESSED events since offset callbacks
        # are supplementary and fire even after the main event occurred
        if offset == timedelta():
            status_filter = EventStatus.PENDING
        else:
            status_filter = None  # Query all statuses for offset callbacks

        # Get events in the adjusted time range
        due_events = Event.objects.get_events(
            from_date=adjusted_since,
            to_date=adjusted_to,
            status=status_filter,
            include_immediate=(offset == timedelta()),  # Only include immediate for zero offset
        )

        processed_count = 0

        for event in due_events:
            try:
                if offset == timedelta():
                    # Zero offset: mark as occurred (triggers callbacks through save)
                    processed_count += self._process_zero_offset_event(event)
                else:
                    # Non-zero offset: fire callbacks directly
                    processed_count += self._process_offset_event(event, offset)
            except Exception as e:
                logger.error(f"Failed to process event {event.id} for offset {offset}: {e}")

        return {
            "found": len(due_events),
            "processed": processed_count,
            "offset": str(offset),
        }

    def _process_zero_offset_event(self, event: Event) -> int:
        """Process an event with zero offset by marking it as occurred."""
        with transaction.atomic():
            locked_event = Event.objects.select_for_update(skip_locked=True).filter(
                id=event.id,
                status=EventStatus.PENDING
            ).first()

            if locked_event:
                locked_event.mark_as_occurred()
                return 1
        return 0

    def _process_offset_event(self, event: Event, offset: timedelta) -> int:
        """Process an event with non-zero offset by firing callbacks directly.

        This doesn't change the event status - offset callbacks are supplementary
        to the main event occurrence. Once processed, won't re-fire even if event moves.
        """
        with transaction.atomic():
            # Check if already processed for this offset
            already_processed = ProcessedEventOffset.objects.filter(
                event=event,
                offset=offset
            ).exists()

            if already_processed:
                return 0

            # Fire callbacks for this offset
            callback_registry.notify(event, EventPhase.OCCURRED, offset=offset)

            # Record that we processed this offset
            ProcessedEventOffset.objects.create(
                event=event,
                offset=offset
            )

            return 1

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
