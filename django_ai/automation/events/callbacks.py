from typing import Callable, Dict, List, Any, Protocol, Union, Optional
from dataclasses import dataclass, field
from datetime import timedelta
import uuid
from enum import Enum
import logging

from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)

# Type alias for offset - accepts both timedelta and relativedelta
# timedelta: fixed durations (hours, minutes, seconds)
# relativedelta: calendar-aware durations (months, years, weekdays)
OffsetType = Union[timedelta, relativedelta]

# Type for debounce key function - takes event, returns grouping key
DebounceKeyFunc = Callable[["Event"], str]


class EventPhase(Enum):
    """Different phases when callbacks can be triggered"""

    CREATED = "created"  # When event is first created
    OCCURRED = "occurred"  # When event actually happens (at its due time)
    CANCELLED = "cancelled"  # When event is cancelled


class EventCallback(Protocol):
    """Protocol for event callbacks"""

    def __call__(self, event: "Event") -> None:
        """Handle an event notification"""
        ...


class DebouncedEventCallback(Protocol):
    """Protocol for debounced event callbacks - receives list of events"""

    def __call__(self, events: List["Event"]) -> None:
        """Handle batched event notifications"""
        ...


@dataclass
class CallbackRegistration:
    """Registration info for a callback"""

    callback: Union[EventCallback, DebouncedEventCallback]
    event_name: str = "*"  # Specific event name or "*" for all
    phase: EventPhase = EventPhase.OCCURRED
    namespace: str = "*"  # Specific event namespace or "*" for all
    offset: OffsetType = field(default_factory=timedelta)  # Offset from event time
    _id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Debounce settings (None = no debounce, execute immediately)
    debounce_key: Optional[DebounceKeyFunc] = None
    debounce_window: Optional[timedelta] = None

    @property
    def is_debounced(self) -> bool:
        return self.debounce_key is not None and self.debounce_window is not None


class EventCallbackRegistry:
    """Registry for event callbacks with filtering"""

    def __init__(self):
        self._callbacks: Dict[int, CallbackRegistration] = {}
        self._enabled = True

    def register(
        self,
        callback: Union[EventCallback, DebouncedEventCallback],
        event_name: str = "*",
        namespace: str = "*",
        phase: EventPhase = EventPhase.OCCURRED,
        offset: OffsetType = None,
        debounce_key: Optional[DebounceKeyFunc] = None,
        debounce_window: Optional[timedelta] = None,
    ) -> None:
        """Register a callback for events.

        Args:
            callback: Function to call when event matches
            event_name: Event name to match, or "*" for all
            namespace: Namespace to match, or "*" for all
            phase: Event phase to trigger on
            offset: Time offset from event time
            debounce_key: Function that takes an event and returns a grouping key.
                         Events with the same key are batched together.
            debounce_window: How long to wait for more events before executing.
                            Each new event extends the window.

        For debounced callbacks:
            - Both debounce_key and debounce_window must be provided
            - Callback receives `events: list[Event]` instead of single event
        """
        # Validate debounce params
        if (debounce_key is None) != (debounce_window is None):
            raise ValueError(
                "Both debounce_key and debounce_window must be provided together"
            )

        # Debounce only works with immediate callbacks (no offset)
        if debounce_key is not None and offset:
            raise ValueError(
                "Debounced callbacks cannot have an offset - debounce only works for immediate events"
            )

        callback_id: int = id(callback)
        registration = CallbackRegistration(
            callback=callback,
            event_name=event_name,
            namespace=namespace,
            phase=phase,
            offset=offset or timedelta(),
            debounce_key=debounce_key,
            debounce_window=debounce_window,
        )

        self._callbacks[callback_id] = registration

    def unregister(self, callback: EventCallback) -> bool:
        callback_id = id(callback)
        if callback_id in self._callbacks:
            del self._callbacks[callback_id]
            return True
        return False

    def get_by_id(self, registration_id: str) -> Optional[CallbackRegistration]:
        """Look up a callback registration by its unique ID."""
        for reg in self._callbacks.values():
            if reg._id == registration_id:
                return reg
        return None

    def notify(
        self, event: "Event", phase: EventPhase, offset: OffsetType = None
    ) -> None:
        """Notify all matching callbacks about an event

        Args:
            event: The event to notify about
            phase: The phase (CREATED, OCCURRED, CANCELLED)
            offset: If provided, only notify callbacks registered for this offset.
                   If None, notify callbacks with zero offset (immediate).
        """
        if not self._enabled:
            return

        target_offset = offset or timedelta()

        # Find matching callbacks
        matching = []
        for reg in self._callbacks.values():
            if (
                reg.phase == phase
                and reg.offset == target_offset
                and (reg.event_name == "*" or reg.event_name == event.event_name)
                and (reg.namespace == "*" or reg.namespace == event.namespace)
            ):
                matching.append(reg)

        # Execute callbacks
        for reg in matching:
            try:
                if reg.is_debounced:
                    # Schedule via debounce - collect event IDs
                    self._schedule_debounced(reg, event)
                else:
                    # Execute immediately
                    reg.callback(event)
            except Exception as e:
                logger.error(
                    f"Event callback {reg.callback} failed for event {event.id}: {e}",
                    exc_info=True,
                )

    def _schedule_debounced(self, reg: CallbackRegistration, event: "Event") -> None:
        """Schedule a debounced callback execution."""
        from django_ai.automation.timers.core import debounce_task

        # Generate the debounce key for this event
        debounce_key = reg.debounce_key(event)

        # Use the dispatcher task which will look up the callback by ID
        debounce_task(
            task_name="django_ai.automation.events.callbacks.dispatch_debounced_callback",
            debounce_key=f"callback:{reg._id}:{debounce_key}",
            debounce_window=reg.debounce_window,
            args=[reg._id],
            collect=event.id,  # Collect event IDs
        )

    def get_registered_offsets(self, phase: EventPhase = EventPhase.OCCURRED) -> set:
        """Get all unique offsets registered for a given phase.

        Used by the event processor to know which offsets to poll for.
        """
        return {
            reg.offset
            for reg in self._callbacks.values()
            if reg.phase == phase
        }

    def disable(self):
        """Temporarily disable all callbacks"""
        self._enabled = False

    def enable(self):
        """Re-enable callbacks"""
        self._enabled = True

    def clear(self):
        """Remove all callbacks (useful for testing)"""
        self._callbacks.clear()

    def list_callbacks(self) -> List[Dict[str, Any]]:
        """List all registered callbacks for debugging"""
        return [
            {
                "callback": str(reg.callback),
                "event_name": reg.event_name,
                "phase": reg.phase.value,
            }
            for reg in self._callbacks.values()
        ]


# Global registry instance
callback_registry = EventCallbackRegistry()


# Convenience decorators
def on_event_base(
    event_name: str = "*",
    namespace: str = "*",
    phase: EventPhase = EventPhase.OCCURRED,
    offset: OffsetType = None,
):
    """Base decorator to register event callbacks"""

    def decorator(func: Callable[["Event"], None]):
        callback_registry.register(func, event_name, namespace, phase, offset)
        return func

    return decorator


def on_event_created(event_name: str = "*", namespace: str = "*"):
    return on_event_base(event_name, namespace, EventPhase.CREATED)


def on_event(event_name: str = "*", namespace: str = "*", offset: OffsetType = None):
    """Decorator for when events occur (default behavior)

    Args:
        event_name: Event name to listen for, or "*" for all
        namespace: Namespace to filter by, or "*" for all
        offset: Offset from event.at time. Accepts timedelta or relativedelta.
               Negative = before, positive = after.
               Examples:
                 - timedelta(hours=-1): fires 1 hour BEFORE event time
                 - relativedelta(months=-1): fires 1 month BEFORE event time
    """
    return on_event_base(event_name, namespace, EventPhase.OCCURRED, offset)


def on_event_cancelled(event_name: str = "*", namespace: str = "*"):
    return on_event_base(event_name, namespace, EventPhase.CANCELLED)


def on_event_debounced(
    debounce_key: DebounceKeyFunc,
    debounce_window: timedelta,
    event_name: str = "*",
    namespace: str = "*",
):
    """Decorator for debounced event callbacks.

    Debounced callbacks batch multiple events together. The callback receives
    `events: list[Event]` instead of a single event.

    Args:
        debounce_key: Function that takes an event and returns a grouping key.
                     Events with the same key are batched together.
        debounce_window: How long to wait for more events before executing.
                        Each new event extends the window.
        event_name: Event name to listen for, or "*" for all
        namespace: Namespace to filter by, or "*" for all

    Example:
        @on_event_debounced(
            debounce_key=lambda e: f"user:{e.entity.user_id}",
            debounce_window=timedelta(seconds=1),
            event_name="message_sent",
        )
        def handle_messages(events: list[Event]):
            # Called once with all batched events
            print(f"Processing {len(events)} messages")
    """
    def decorator(func: Callable[[List["Event"]], None]):
        callback_registry.register(
            func,
            event_name=event_name,
            namespace=namespace,
            phase=EventPhase.OCCURRED,
            offset=None,
            debounce_key=debounce_key,
            debounce_window=debounce_window,
        )
        return func

    return decorator


def dispatch_debounced_callback(registration_id: str, collected_args: List[int] = None):
    """
    Dispatcher task called by the timers app when a debounced callback fires.

    Args:
        registration_id: The callback registration's unique ID
        collected_args: List of event IDs that were batched together
    """
    from .models import Event

    # Look up the callback registration
    reg = callback_registry.get_by_id(registration_id)
    if reg is None:
        logger.warning(f"Debounced callback {registration_id} not found in registry")
        return

    # Fetch the events
    event_ids = collected_args or []
    if not event_ids:
        return

    events = list(Event.objects.filter(id__in=event_ids))

    if not events:
        return

    # Call the callback with the list of events
    try:
        reg.callback(events)
    except Exception as e:
        logger.error(
            f"Debounced callback {reg.callback} failed for events {event_ids}: {e}",
            exc_info=True,
        )
