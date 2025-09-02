from typing import Callable, Dict, List, Any, Protocol
from dataclasses import dataclass, field
import uuid
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
    namespace: str = "*" # Specific event namespace or "*" for all
    _id: str = field(default_factory=lambda: str(uuid.uuid4()))


class EventCallbackRegistry:
    """Registry for event callbacks with filtering"""

    def __init__(self):
        self._callbacks: Dict[int, CallbackRegistration] = {}
        self._enabled = True

    def register(
        self,
        callback: EventCallback,
        event_name: str = "*",
        entity_type: str = "*",
        namespace: str = "*",
        phase: EventPhase = EventPhase.OCCURRED,
    ) -> None:
        """Register a callback for events"""
        callback_id: int = id(callback)
        registration = CallbackRegistration(
            callback=callback,
            event_name=event_name,
            entity_type=entity_type,
            namespace=namespace,
            phase=phase,
        )

        self._callbacks[callback_id] = registration

    def unregister(self, callback: EventCallback) -> bool:
        callback_id = id(callback)
        if callback_id in self._callbacks:
            del self._callbacks[callback_id]
            return True
        return False

    def notify(self, event: "Event", phase: EventPhase) -> None:
        """Notify all matching callbacks about an event"""
        if not self._enabled:
            return

        entity_type = event.model_type.model

        # Find matching callbacks
        matching = []
        for reg in self._callbacks.values():
            if (
                reg.phase == phase
                and (reg.event_name == "*" or reg.event_name == event.event_name)
                and (reg.entity_type == "*" or reg.entity_type == entity_type)
                and (reg.namespace == "*" or reg.namespace == event.namespace)
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
    namespace: str = "*",
    phase: EventPhase = EventPhase.OCCURRED,
):
    """Base decorator to register event callbacks"""

    def decorator(func: Callable[["Event"], None]):
        callback_registry.register(func, event_name, entity_type, namespace, phase)
        return func

    return decorator


def on_event_created(event_name: str = "*", entity_type: str = "*", namespace: str = "*"):
    return on_event_base(event_name, entity_type, namespace, EventPhase.CREATED)


def on_event(event_name: str = "*", entity_type: str = "*", namespace: str = "*"):
    """Decorator for when events occur (default behavior)"""
    return on_event_base(event_name, entity_type, namespace, EventPhase.OCCURRED)


def on_event_cancelled(event_name: str = "*", entity_type: str = "*", namespace: str = "*"):
    return on_event_base(event_name, entity_type, namespace, EventPhase.CANCELLED)
