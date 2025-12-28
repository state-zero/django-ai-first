from django.db import models
from django.utils import timezone


class TimedTask(models.Model):
    """
    Durable history of executed tasks (audit log).

    Pending tasks live in the Cache to avoid DB locking issues.
    Only completed/executed tasks are stored here for history and debugging.
    """

    # Task definition
    task_name = models.CharField(max_length=255)
    task_args = models.JSONField(default=list)
    task_kwargs = models.JSONField(default=dict)
    collected_args = models.JSONField(default=list)
    debounce_key = models.CharField(max_length=255, null=True, blank=True)

    # Timing
    run_at = models.DateTimeField()
    dispatched_at = models.DateTimeField(default=timezone.now)

    # Execution result
    success = models.BooleanField(default=True)
    error_log = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-dispatched_at"]
        indexes = [
            models.Index(fields=["debounce_key"]),
            models.Index(fields=["dispatched_at"]),
            models.Index(fields=["task_name"]),
        ]

    def __str__(self):
        status = "OK" if self.success else "FAIL"
        return f"[{status}] {self.task_name} @ {self.dispatched_at}"
