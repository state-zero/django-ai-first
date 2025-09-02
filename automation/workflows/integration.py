import logging
from ..events.callbacks import on_event
from ..events.models import Event
from .core import engine

logger = logging.getLogger(__name__)


@on_event()
def handle_event_for_workflows(event: Event) -> None:
    """Handle events when they occur - start event workflows and signal waiting workflows"""

    # 1. Start event-triggered workflows (replaces old automations)
    try:
        workflows_started = engine.handle_event_occurred(event)

        if workflows_started:
            logger.info(
                f"Started {len(workflows_started)} workflows for event {event.id}"
            )
    except Exception as e:
        logger.error(
            f"Error starting workflows for event {event.id}: {e}", exc_info=True
        )

    # 2. Signal existing workflows waiting for this event
    try:
        signal_name = f"event:{event.event_name}"
        payload = {
            "event_id": event.id,
            "event_name": event.event_name,
            "entity_id": event.entity_id,
            "event_at": event.at,
        }

        engine.signal(signal_name, payload)
    except Exception as e:
        logger.error(
            f"Error signaling workflows for event {event.id}: {e}", exc_info=True
        )