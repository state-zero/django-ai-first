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

    def get_watched_values_hash(self, instance: models.Model) -> Optional[str]:
        """Get a hash of watched field values for change detection, None if no watch_fields.

        Uses get_prep_value() which is Django's canonical way to convert any field
        value to its database representation - handles all field types consistently.
        """
        import hashlib

        if not self.watch_fields:
            return None

        hasher = hashlib.sha256()
        for field_name in sorted(self.watch_fields):  # sort for deterministic ordering
            field = instance._meta.get_field(field_name)
            # get_prep_value converts to DB-storage form (handles all types)
            db_value = field.get_prep_value(field.value_from_object(instance))
            hasher.update(f"{field_name}:{db_value!r}".encode('utf-8'))
        return hasher.hexdigest()
