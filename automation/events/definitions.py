from django.db import models
from typing import List, Optional, Callable, Any
from datetime import datetime


class EventDefinition:
    def __init__(
        self,
        name: str,
        date_field: Optional[str] = None,
        condition: Optional[Callable[[models.Model], bool]] = None,
    ) -> None:
        self.name = name
        self.date_field = date_field  # None for immediate events
        self.condition = condition or (lambda instance: True)

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
