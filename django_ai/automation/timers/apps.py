from django.apps import AppConfig


class TimersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_ai.automation.timers"
    label = "timers"

    def ready(self):
        """Start background thread if TIMERS_IN_PROCESS=True."""
        from django_ai.conf import get_timers_in_process

        # Only start if explicitly set to True
        if get_timers_in_process() is True:
            from .core import start_sync_thread
            start_sync_thread()
