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
        Get comprehensive display metadata for the current step, this is used exclusively in the frontend
        when presenting the workflow to the user. It does not impact any of the backend functionality and you
        should not create backend code that depends on the behaviour of this function.
        
        Returns display metadata including:
        - Step type (action, waiting, automated)
        - Step display info (title, description, field groups, field configs)
        - Completion display (if completed)
        - Waiting display (if suspended)
        """
        from .core import _workflows
        
        result = {
            'status': self.status,
            'current_step': self.current_step,
            'step_type': None,
            'step_display': None,
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
                # Don't return early - still need step type and display below
        
        # Get current step info
        if not self.current_step:
            return result
        
        workflow_cls = _workflows.get(self.name)
        if not workflow_cls:
            return result
        
        step_method = getattr(workflow_cls, self.current_step, None)
        if not step_method:
            return result
        
        # Determine step type
        if hasattr(step_method, "_has_statezero_action"):
            result['step_type'] = StepType.ACTION
        elif hasattr(step_method, "_is_event_wait_step"):
            result['step_type'] = StepType.WAITING
        else:
            result['step_type'] = StepType.AUTOMATED
        
        # Get step display metadata
        if getattr(step_method, '_display_metadata'):
            metadata = step_method._display_metadata
            
            step_display = {
                'display_title': metadata.display_title,
                'display_description': metadata.display_description,
                'field_groups': [
                    {
                        'display_title': fg.display_title,
                        'display_description': fg.display_description,
                        'field_names': fg.field_names
                    }
                    for fg in (metadata.field_groups or [])
                ] if metadata.field_groups else []
            }
            
            if metadata.field_display_configs:
                step_display['field_display_configs'] = [
                    {
                        'field_name': fc.field_name,
                        'display_component': fc.display_component,
                        'filter_queryset': fc.filter_queryset,
                        'display_help_text': fc.display_help_text,
                    }
                    for fc in metadata.field_display_configs
                ]
            
            result['step_display'] = step_display
        
        return result

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