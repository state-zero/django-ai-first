from django.db import models
from typing import Optional

class WorkflowStatus(models.TextChoices):
    RUNNING = "running", "Running"
    WAITING = "waiting", "Waiting"  # For time-based sleep
    SUSPENDED = "suspended", "Suspended"  # For external triggers (actions, events)
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"

class StepType(models.TextChoices):
    ACTION = "action", "StateZero Action"
    AUTOMATED = "automated", "Automated"
    WAITING = "waiting", "Waiting"

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

    # Progress
    progress = models.FloatField(default=0.0)

    @property
    def current_step_type(self) -> Optional[str]:
        """Determine the type of the current step by introspecting the workflow class"""
        from .core import _workflows
        
        # Get workflow class and step method, return None if not found
        workflow_cls = _workflows.get(self.name)
        if not workflow_cls:
            return None
        
        step_method = getattr(workflow_cls, self.current_step, None)
        if not step_method:
            return None
        
        # Check decorators in priority order
        if hasattr(step_method, "_has_statezero_action"):
            return StepType.ACTION
        
        if hasattr(step_method, "_is_event_wait_step"):
            return StepType.WAITING
        
        return StepType.AUTOMATED

    class Meta:
        indexes = [
            models.Index(fields=["status", "waiting_signal"]),
            models.Index(fields=["status", "wake_at"]),
            models.Index(fields=["triggered_by_event_id"]),
        ]

class StepExecution(models.Model):
    """Lightweight history of completed/failed workflow steps"""
    workflow_run = models.ForeignKey(
        WorkflowRun, 
        on_delete=models.CASCADE, 
        related_name='step_executions'
    )
    step_name = models.CharField(max_length=100)
    status = models.CharField(
        max_length=20,
        choices=[
            ('completed', 'Completed'),
            ('failed', 'Failed'),
        ]
    )
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['workflow_run', 'created_at']),
        ]
    
    def __str__(self):
        return f"{self.workflow_run.name} - {self.step_name} ({self.status})"