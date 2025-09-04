from django.apps import AppConfig
from django.db.models.signals import post_migrate


class QueuesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_ai.automation.queues"

    def ready(self):
        from .scheduling import ensure_q2_schedules

        def _after_migrate(**kwargs):
            try:
                ensure_q2_schedules()
            except Exception:
                # Don't block startup on schedule creation
                pass

        post_migrate.connect(_after_migrate, sender=self.__class__)
