"""
Debounce key resolution for event-triggered workflows and handlers.
"""

from django.template import Template, Context
import re


def resolve_debounce_key(template: str, event) -> str:
    """
    Resolve a debounce key template against an event's entity.

    The template can reference:
    - `entity` - the event's entity object
    - `event` - the event itself

    Examples:
        "{entity.id}" -> "123"
        "{entity.primary_guest.id}" -> "456"
        "{event.event_name}:{entity.id}" -> "booking_created:123"

    Args:
        template: Template string with {placeholders}
        event: The Event object

    Returns:
        Resolved debounce key string
    """
    try:
        entity = event.entity
    except Exception:
        entity = None

    # Convert {placeholders} to Django template syntax
    django_template = re.sub(r'\{([^}]+)\}', r'{{ \1 }}', template)

    template_obj = Template(django_template)
    context = Context({"entity": entity, "event": event})

    return template_obj.render(context).strip()
