from typing import Callable, Dict, List, Any, Protocol, Union, Optional
from dataclasses import dataclass, field
from datetime import timedelta
import uuid
from enum import Enum
import inspect
import logging

from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)

# Type alias for offset - accepts both timedelta and relativedelta
# timedelta: fixed durations (hours, minutes, seconds)
# relativedelta: calendar-aware durations (months, years, weekdays)
OffsetType = Union[timedelta, relativedelta]


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


@dataclass
class CallbackRegistration:
    """Registration info for a callback"""

    callback: EventCallback
    event_name: str = "*"  # Specific event name or "*" for all
    phase: EventPhase = EventPhase.OCCURRED
    namespace: str = "*"  # Specific event namespace or "*" for all
    offset: OffsetType = field(default_factory=timedelta)  # Offset from event time
    debounce: Optional[timedelta] = None  # Debounce delay
    debounce_key: Optional[str] = None  # Template for debounce key
    callback_key: Optional[str] = None  # Unique key to look up callback later
    _id: str = field(default_factory=lambda: str(uuid.uuid4()))


class EventCallbackRegistry:
    """Registry for event callbacks with filtering"""

    def __init__(self):
        self._callbacks: Dict[int, CallbackRegistration] = {}
        self._debounced_callbacks: Dict[str, EventCallback] = {}  # callback_key -> callback
        self._enabled = True

    def register(
        self,
        callback: EventCallback,
        event_name: str = "*",
        namespace: str = "*",
        phase: EventPhase = EventPhase.OCCURRED,
        offset: OffsetType = None,
        debounce: Optional[timedelta] = None,
        debounce_key: Optional[str] = None,
    ) -> None:
        """Register a callback for events"""
        callback_id: int = id(callback)

        # Generate callback key for debounced callbacks
        callback_key = None
        if debounce is not None:
            callback_key = f"{callback.__module__}.{callback.__name__}"
            self._debounced_callbacks[callback_key] = callback

        registration = CallbackRegistration(
            callback=callback,
            event_name=event_name,
            namespace=namespace,
            phase=phase,
            offset=offset or timedelta(),
            debounce=debounce,
            debounce_key=debounce_key,
            callback_key=callback_key,
        )

        self._callbacks[callback_id] = registration

    def get_callback_by_key(self, callback_key: str) -> Optional[EventCallback]:
        """Look up a debounced callback by its key."""
        return self._debounced_callbacks.get(callback_key)

    def unregister(self, callback: EventCallback) -> bool:
        callback_id = id(callback)
        if callback_id in self._callbacks:
            del self._callbacks[callback_id]
            return True
        return False

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
                if reg.debounce is not None:
                    # Route through debounce system
                    self._debounce_callback(reg, event)
                else:
                    # Execute immediately
                    reg.callback(event)
            except Exception as e:
                logger.error(
                    f"Event callback {reg.callback} failed for event {event.id}: {e}",
                    exc_info=True,
                )

    def _debounce_callback(self, reg: CallbackRegistration, event: "Event") -> None:
        """Route a callback through the debounce system."""
        from ..queues.debounce import resolve_debounce_key
        from ..workflows.core import engine

        # Resolve debounce key
        if reg.debounce_key:
            resolved_key = resolve_debounce_key(reg.debounce_key, event)
            debounce_key = f"callback:{reg.callback_key}:{resolved_key}"
        else:
            debounce_key = f"callback:{reg.callback_key}"

        engine.executor.debounce_task(
            debounce_key,
            "execute_event_callback",
            reg.callback_key,
            delay=reg.debounce,
            event_id=event.id,
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
    debounce: Optional[timedelta] = None,
    debounce_key: Optional[str] = None,
):
    """Base decorator to register event callbacks"""

    def decorator(func: Callable[["Event"], None]):
        # Validate debounced callbacks have 'events' parameter
        if debounce is not None:
            params = inspect.signature(func).parameters
            if "events" not in params:
                raise ValueError(
                    f"Callback '{func.__name__}' with debounce must have 'events' "
                    f"parameter to receive accumulated events. "
                    f"Change signature to: def {func.__name__}(events): ..."
                )

        callback_registry.register(
            func, event_name, namespace, phase, offset, debounce, debounce_key
        )
        return func

    return decorator


def on_event_created(event_name: str = "*", namespace: str = "*"):
    return on_event_base(event_name, namespace, EventPhase.CREATED)


def on_event(
    event_name: str = "*",
    namespace: str = "*",
    offset: OffsetType = None,
    debounce: Optional[timedelta] = None,
    debounce_key: Optional[str] = None,
):
    """Decorator for when events occur (default behavior)

    Args:
        event_name: Event name to listen for, or "*" for all
        namespace: Namespace to filter by, or "*" for all
        offset: Offset from event.at time. Accepts timedelta or relativedelta.
               Negative = before, positive = after.
               Examples:
                 - timedelta(hours=-1): fires 1 hour BEFORE event time
                 - relativedelta(months=-1): fires 1 month BEFORE event time
        debounce: If set, debounce the callback by this duration. Multiple events
                 within the debounce window are accumulated and passed as a list.
        debounce_key: Template for grouping debounced events (e.g., "{entity.id}").
                     Events with the same resolved key are debounced together.
    """
    return on_event_base(
        event_name, namespace, EventPhase.OCCURRED, offset, debounce, debounce_key
    )


def on_event_cancelled(event_name: str = "*", namespace: str = "*"):
    return on_event_base(event_name, namespace, EventPhase.CANCELLED)
