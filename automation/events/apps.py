from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "automation.events"

    def ready(self):
        # Import signal handlers
        from . import signals
