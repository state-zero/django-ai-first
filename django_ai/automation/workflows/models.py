from django.db import models


class WorkflowStatus(models.TextChoices):
    RUNNING = "running", "Running"
    WAITING = "waiting", "Waiting"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class WorkflowRun(models.Model):
    name = models.CharField(max_length=100)
    version = models.CharField(max_length=20)
    current_step = models.CharField(max_length=100)
    data = models.JSONField(default=dict)
    status = models.CharField(
        max_length=20, choices=WorkflowStatus.choices, default=WorkflowStatus.RUNNING
    )

    triggered_by_event_id = models.IntegerField(null=True, blank=True)

    # Timing
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    wake_at = models.DateTimeField(null=True, blank=True)

    # Current wait state
    waiting_signal = models.CharField(max_length=100, blank=True)
    on_timeout_step = models.CharField(max_length=100, blank=True)

    # Error tracking
    error = models.TextField(blank=True)
    retry_count = models.IntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["status", "waiting_signal"]),
            models.Index(fields=["status", "wake_at"]),
            models.Index(fields=["triggered_by_event_id"]),
        ]
