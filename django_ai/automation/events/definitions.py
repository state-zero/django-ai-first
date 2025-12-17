from django.db import models
from typing import List, Optional, Callable, Any, Union
from datetime import datetime


class EventTrigger(models.TextChoices):
    """When an event should fire in the model lifecycle"""
    CREATE = "create", "Create"   # Fire only on first save (default, backward compatible)
    UPDATE = "update", "Update"   # Fire only on subsequent saves
    DELETE = "delete", "Delete"   # Fire on deletion


class EventDefinition:

    def __init__(
        self,
        name: str,
        date_field: Optional[str] = None,
        condition: Optional[Callable[[models.Model], bool]] = None,
        namespace: Union[str, Callable[[models.Model], str]] = "*",
        trigger: Union[EventTrigger, List[EventTrigger]] = EventTrigger.CREATE,
        watch_fields: Optional[List[str]] = None,
    ) -> None:
        self.name = name
        self.date_field = date_field  # None for immediate events
        self.condition = condition or (lambda instance: True)
        self._namespace = namespace
        self.watch_fields = watch_fields
        # Normalize trigger to always be a list
        if isinstance(trigger, EventTrigger):
            self.triggers = [trigger]
        elif isinstance(trigger, list) and all(isinstance(t, EventTrigger) for t in trigger):
            self.triggers = trigger
        else:
            raise ValueError(
                f"trigger must be an EventTrigger or List[EventTrigger], got {type(trigger)}"
            )

    def get_namespace(self, instance: models.Model) -> str:
        """Get the namespace for this event given a model instance"""
        if callable(self._namespace):
            return self._namespace(instance)
        return self._namespace

    def should_create(self, instance: models.Model) -> bool:
        """Check if this event should be valid for the given instance"""
        return self.condition(instance)

    def get_date_value(self, instance: models.Model) -> Optional[datetime]:
        """Get the date value from the instance for scheduling, None for immediate events"""
        if self.date_field is None:
            return None  # Immediate event
        return getattr(instance, self.date_field)

    def is_immediate(self) -> bool:
        """Check if this is an immediate event (no date field)"""
        return self.date_field is None

    def should_trigger(self, trigger: EventTrigger) -> bool:
        """Check if this event should fire for the given trigger"""
        return trigger in self.triggers

    def get_watched_values(self, instance: models.Model) -> Optional[dict]:
        """Get current values of watched fields from instance, None if no watch_fields"""
        import json
        from django.core.serializers.json import DjangoJSONEncoder

        if not self.watch_fields:
            return None
        values = {}
        for field in self.watch_fields:
            value = getattr(instance, field, None)
            # Convert FK to pk
            if hasattr(value, 'pk'):
                value = value.pk
            # Use Django's encoder to handle UUID, datetime, Decimal, etc.
            values[field] = json.loads(json.dumps(value, cls=DjangoJSONEncoder))
        return values
