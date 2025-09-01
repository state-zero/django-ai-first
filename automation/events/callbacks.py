from typing import Callable, Dict, List, Any, Protocol
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


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
    entity_type: str = "*"  # Specific entity type or "*" for all
    phase: EventPhase = EventPhase.OCCURRED


class EventCallbackRegistry:
    """Registry for event callbacks with filtering"""

    def __init__(self):
        self._callbacks: List[CallbackRegistration] = []
        self._enabled = True

    def register(
        self,
        callback: EventCallback,
        event_name: str = "*",
        entity_type: str = "*",
        phase: EventPhase = EventPhase.OCCURRED,
    ) -> None:
        """Register a callback for events"""
        registration = CallbackRegistration(
            callback=callback,
            event_name=event_name,
            entity_type=entity_type,
            phase=phase,
        )

        self._callbacks.append(registration)

    def unregister(self, callback: EventCallback) -> bool:
        """Remove a callback from registry"""
        original_len = len(self._callbacks)
        self._callbacks = [reg for reg in self._callbacks if reg.callback != callback]
        return len(self._callbacks) != original_len

    def notify(self, event: "Event", phase: EventPhase) -> None:
        """Notify all matching callbacks about an event"""
        if not self._enabled:
            return

        entity_type = event.model_type.model

        # Find matching callbacks
        matching = []
        for reg in self._callbacks:
            if (
                reg.phase == phase
                and (reg.event_name == "*" or reg.event_name == event.event_name)
                and (reg.entity_type == "*" or reg.entity_type == entity_type)
            ):
                matching.append(reg)

        # Execute callbacks
        for reg in matching:
            try:
                reg.callback(event)
            except Exception as e:
                logger.error(
                    f"Event callback {reg.callback} failed for event {event.id}: {e}",
                    exc_info=True,
                )

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
                "entity_type": reg.entity_type,
                "phase": reg.phase.value,
            }
            for reg in self._callbacks
        ]


# Global registry instance
callback_registry = EventCallbackRegistry()


# Convenience decorators
def on_event_base(
    event_name: str = "*",
    entity_type: str = "*",
    phase: EventPhase = EventPhase.OCCURRED,
):
    """Base decorator to register event callbacks"""

    def decorator(func: Callable[["Event", EventPhase], None]):
        callback_registry.register(func, event_name, entity_type, phase)
        return func

    return decorator


def on_event_created(event_name="*", entity_type="*"):
    return on_event_base(event_name, entity_type, EventPhase.CREATED)


def on_event(event_name: str = "*", entity_type: str = "*"):
    """Decorator for when events occur (default behavior)"""
    return on_event_base(event_name, entity_type, EventPhase.OCCURRED)


def on_event_cancelled(event_name="*", entity_type="*"):
    return on_event_base(event_name, entity_type, EventPhase.CANCELLED)
