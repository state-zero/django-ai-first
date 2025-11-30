from django.db import models
from typing import Optional, Dict

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

    # Subworkflow tracking
    parent_run_id = models.IntegerField(null=True, blank=True, help_text="Parent workflow if this is a subworkflow")
    active_subworkflow_run_id = models.IntegerField(null=True, blank=True, help_text="Currently active child subworkflow")

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

    # --------------- USER DISPLAY INFO -------------------------------- #

    # Progress
    progress = models.FloatField(default=0.0)

    # Customised success message for the frontend
    completion_display = models.JSONField(
        default=dict,
        blank=True,
        help_text="Display metadata for completion state: {display_title, display_subtitle}"
    )
    
    # Displays on the frontend why the workflow is waiting
    waiting_display = models.JSONField(
        default=dict,
        blank=True,
        help_text="Display metadata for waiting state: {display_title, display_subtitle, display_waiting_for}"
    )

    @property
    def current_step_display(self) -> Optional[Dict]:
        """
        Get workflow runtime display metadata for the current step.

        This is used exclusively in the frontend when presenting workflow state to the user.
        It does not impact any backend functionality.

        Returns runtime display metadata including:
        - Step type (action, waiting, automated)
        - Completion display (if completed)
        - Waiting display (if suspended)

        Note: Static display metadata (field groups, field configs, etc.) is handled by
        statezero and should be fetched from the statezero schema/action registry in the frontend.
        """
        from .core import _workflows

        result = {
            'status': self.status,
            'current_step': self.current_step,
            'step_type': None,
            'step_title': None,
            'visible': True,  # Default to visible
            'completion_display': None,
            'waiting_display': None
        }

        # Add completion display if completed
        if self.status == WorkflowStatus.COMPLETED and self.completion_display:
            if self.completion_display.get('display_title'):
                result['completion_display'] = self.completion_display
                return result  # Return early, no need for other info

        # Add waiting display if suspended
        if self.status == WorkflowStatus.SUSPENDED and self.waiting_display:
            if self.waiting_display.get('display_title'):
                result['waiting_display'] = self.waiting_display

        # Get current step info for step type
        if not self.current_step:
            return result

        workflow_cls = _workflows.get(self.name)
        if not workflow_cls:
            return result

        step_method = getattr(workflow_cls, self.current_step, None)
        if not step_method:
            return result

        # Get visibility (defaults to True if not set)
        result['visible'] = getattr(step_method, '_step_visible', True)

        # Get step title (defaults to None, frontend can fall back to step name)
        result['step_title'] = getattr(step_method, '_step_title', None)

        # Determine step type
        if hasattr(step_method, "_has_statezero_action"):
            result['step_type'] = StepType.ACTION
            # Include both the simple action name and full path for the frontend
            result['action_name'] = step_method.__name__
            result['action_path'] = getattr(step_method, '_full_action_name', None)
        elif hasattr(step_method, "_is_event_wait_step"):
            result['step_type'] = StepType.WAITING
        else:
            result['step_type'] = StepType.AUTOMATED

        return result

    def cancel(self, reason: Optional[str] = None):
        """
        Cancel this workflow and all its active subflows.

        Args:
            reason: Optional reason for cancellation

        Raises:
            ValueError: If called on a subflow (must cancel from parent)
        """
        # Reject if this is a subflow - must cancel from parent
        if self.parent_run_id:
            raise ValueError(
                "Cannot cancel subflow directly. Cancel the parent workflow instead."
            )

        self._cancel_recursive(reason)

    def _cancel_recursive(self, reason: Optional[str] = None):
        """Internal: Cancel this workflow and its children recursively."""
        from .core import _workflows, _build_step_display

        # Skip if already in terminal state
        if self.status in [
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        ]:
            return

        # First, cancel any active child subflow (depth-first)
        if self.active_subworkflow_run_id:
            try:
                child_run = WorkflowRun.objects.get(id=self.active_subworkflow_run_id)
                child_run._cancel_recursive(reason)
            except WorkflowRun.DoesNotExist:
                pass

        # Now cancel this workflow
        workflow_cls = _workflows.get(self.name)

        StepExecution.objects.create(
            workflow_run=self,
            step_name=self.current_step,
            status='cancelled',
            error=reason or '',
            step_display=_build_step_display(workflow_cls, self.current_step)
        )

        self.status = WorkflowStatus.CANCELLED
        if reason:
            self.error = reason
        self.waiting_signal = ''
        self.wake_at = None
        self.save()

    def retry_current_step(self):
        """
        Retry the current step, resetting error state.

        Works on RUNNING, WAITING, SUSPENDED, and FAILED (resurrect) states.
        If the workflow has an active subflow, that subflow is cancelled first.

        Raises:
            ValueError: If called on a subflow or on COMPLETED/CANCELLED state
        """
        from .core import engine

        # Reject if this is a subflow
        if self.parent_run_id:
            raise ValueError(
                "Cannot retry subflow directly. Retry the parent workflow instead."
            )

        # Reject terminal states (except FAILED which we resurrect)
        if self.status == WorkflowStatus.COMPLETED:
            raise ValueError("Cannot retry a completed workflow")
        if self.status == WorkflowStatus.CANCELLED:
            raise ValueError("Cannot retry a cancelled workflow")

        # Cancel any active subflow first
        if self.active_subworkflow_run_id:
            try:
                child_run = WorkflowRun.objects.get(id=self.active_subworkflow_run_id)
                child_run._cancel_recursive("Parent workflow retried")
            except WorkflowRun.DoesNotExist:
                pass
            self.active_subworkflow_run_id = None

        # Reset state for retry
        self.status = WorkflowStatus.RUNNING
        self.error = ''
        self.retry_count = 0
        self.waiting_signal = ''
        self.wake_at = None
        self.on_timeout_step = ''
        self.waiting_display = {}
        self.save()

        # Queue the current step for execution
        engine._queue_step(self.id, self.current_step)

    class Meta:
        indexes = [
            models.Index(fields=["status", "waiting_signal"]),
            models.Index(fields=["status", "wake_at"]),
            models.Index(fields=["triggered_by_event_id"]),
            models.Index(fields=["parent_run_id"]),
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
            ('cancelled', 'Cancelled'),
        ]
    )
    error = models.TextField(blank=True)
    step_display = models.JSONField(
        default=dict,
        blank=True,
        help_text="Display metadata captured at execution time: {visible, step_title, step_type}"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['workflow_run', 'created_at']),
        ]
    
    def __str__(self):
        return f"{self.workflow_run.name} - {self.step_name} ({self.status})"