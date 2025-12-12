from datetime import timedelta
from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone


class AgentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class HandlerStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class AgentRun(models.Model):
    """A running agent instance that manages handlers"""

    # Agent identity
    agent_name = models.CharField(max_length=255)
    namespace = models.CharField(max_length=255)

    # Entity reference (optional - for agents spawned by entity events)
    entity_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    entity_id = models.CharField(max_length=50, blank=True)

    # Agent context (Pydantic model serialized as JSON)
    context = models.JSONField(default=dict)

    # Current state
    status = models.CharField(
        max_length=20,
        choices=AgentStatus.choices,
        default=AgentStatus.ACTIVE,
    )

    # Timing
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Note: Singleton enforcement is handled in code, not via DB constraint
        # This allows non-singleton agents to have multiple instances per namespace
        indexes = [
            models.Index(fields=["agent_name", "status"]),
            models.Index(fields=["namespace"]),
            models.Index(fields=["agent_name", "namespace", "status"]),
        ]

    def __str__(self):
        return f"{self.agent_name}:{self.namespace} ({self.status})"


class HandlerExecution(models.Model):
    """Tracks each handler execution for debugging and auditing"""

    agent_run = models.ForeignKey(
        AgentRun,
        on_delete=models.CASCADE,
        related_name="executions",
    )

    # Handler identification
    handler_name = models.CharField(max_length=255)
    event_name = models.CharField(max_length=255)
    event_id = models.IntegerField(null=True, blank=True)
    offset = models.DurationField(default=timedelta)

    # Status tracking
    status = models.CharField(
        max_length=20,
        choices=HandlerStatus.choices,
        default=HandlerStatus.PENDING,
    )

    # Timing
    scheduled_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Error tracking
    error = models.TextField(blank=True)
    attempt_count = models.IntegerField(default=0)

    class Meta:
        ordering = ["-scheduled_at"]
        indexes = [
            models.Index(fields=["agent_run", "status"]),
            models.Index(fields=["status", "scheduled_at"]),
            models.Index(fields=["handler_name", "status"]),
        ]

    def __str__(self):
        return f"{self.agent_run.agent_name}.{self.handler_name} ({self.status})"
