from django.apps import AppConfig


class WorkflowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_ai.automation.workflows"

    def ready(self):
        from . import integration  # event→workflow bridge
        from .core import engine
        from ..queues.q2_executor import Q2Executor

        engine.set_executor(Q2Executor())
