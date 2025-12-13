from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.contrib.contenttypes.models import ContentType
from .models import Event
from .definitions import EventDefinition, EventTrigger
from typing import Any
from django.db import models


def _has_event_definitions(model_class: type) -> bool:
    """Check if a model has valid event definitions"""
    if not hasattr(model_class, "events"):
        return False

    events = getattr(model_class, "events")
    if not hasattr(events, "__iter__"):
        return False

    # Check if it's a non-empty iterable of EventDefinition objects
    try:
        events_list = list(events)
        return len(events_list) > 0 and all(
            isinstance(ed, EventDefinition) for ed in events_list
        )
    except (TypeError, AttributeError):
        return False


@receiver(post_save)
def update_events_on_save(sender: type, instance: models.Model, **kwargs: Any) -> None:
    """Update events when a model instance is saved"""
    if _has_event_definitions(sender):
        created = kwargs.get("created", False)
        trigger = EventTrigger.CREATE if created else EventTrigger.UPDATE
        Event.objects.update_for_instance(instance, trigger=trigger)


@receiver(post_delete)
def handle_events_on_delete(
    sender: type, instance: models.Model, **kwargs: Any
) -> None:
    """Handle DELETE trigger events, then clean up event records"""
    if _has_event_definitions(sender):
        # First, fire any DELETE trigger events
        Event.objects.update_for_instance(instance, trigger=EventTrigger.DELETE)

        # Then clean up all event records for this instance
        content_type = ContentType.objects.get_for_model(sender)
        Event.objects.filter(
            model_type=content_type, entity_id=str(instance.pk)
        ).delete()
