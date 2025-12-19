import logging
import traceback
from datetime import timedelta
from typing import Dict, Any, Optional, Callable, TypeVar, Generic, Union
from django.conf import settings

from pydantic import BaseModel
from dateutil.relativedelta import relativedelta

T = TypeVar("T", bound=BaseModel)
from dataclasses import dataclass
from pydantic import BaseModel
from contextvars import ContextVar
from django.db import transaction
from django.db.models import Q
from .models import WorkflowRun, WorkflowStatus, StepExecution, StepType
from django.utils import timezone
from ...utils.json import safe_model_dump

# Type alias for offset - accepts both timedelta and relativedelta
OffsetType = Union[timedelta, relativedelta]

logger = logging.getLogger(__name__)


def _build_step_display(workflow_cls, step_name: str) -> dict:
    """Build step display metadata from step method attributes."""
    step_method = getattr(workflow_cls, step_name, None) if workflow_cls else None

    result = {
        'visible': True,
        'step_title': None,
        'step_type': None,
    }

    if not step_method:
        return result

    result['visible'] = getattr(step_method, '_step_visible', True)
    result['step_title'] = getattr(step_method, '_step_title', None)

    if hasattr(step_method, "_has_statezero_action"):
        result['step_type'] = StepType.ACTION
    elif hasattr(step_method, "_is_event_wait_step"):
        result['step_type'] = StepType.WAITING
    else:
        result['step_type'] = StepType.AUTOMATED

    return result


# Return types for step functions
@dataclass
class StepResult:
    pass


@dataclass
class Goto(StepResult):
    step: str
    progress: Optional[float] = None  # 0.0-100.0


@dataclass
class Sleep(StepResult):
    duration: timedelta


@dataclass
class Complete(StepResult):
    result: Optional[Dict[str, Any]] = None
    display_title: Optional[str] = None
    display_subtitle: Optional[str] = None


@dataclass
class Fail(StepResult):
    reason: str


@dataclass
class RunSubflow(StepResult):
    workflow_class: type
    on_complete: str
    context_kwargs: Dict[str, Any]
    on_create: Optional[str] = None


@dataclass
class SubflowResult:
    """Result of a completed subworkflow, passed to the on_complete step."""
    status: WorkflowStatus  # COMPLETED, FAILED, or CANCELLED
    context: BaseModel  # The child workflow's final context
    run_id: int  # The child workflow run ID
    error: Optional[str] = None  # Error message if failed/cancelled


# Helper functions
def goto(step: str, progress: Optional[float] = None) -> Goto:
    return Goto(step, progress)


def sleep(duration: timedelta) -> Sleep:
    return Sleep(duration)


def wait_for_event(
    event_name: str,
    timeout: Optional[timedelta] = None,
    on_timeout: Optional[str] = None,
    display_title: Optional[str] = None,
    display_subtitle: Optional[str] = None,
    display_waiting_for: Optional[str] = None,
):
    """
    Decorator to make a step wait for an event before executing.
    
    Args:
        event_name: Name of the event to wait for
        timeout: Optional timeout duration
        on_timeout: Optional step to go to on timeout
        display_title: Title to show while waiting (for frontend)
        display_subtitle: Subtitle to show while waiting (for frontend)
        display_waiting_for: Description of what we're waiting for (for frontend)
    """
    def decorator(func):
        func._is_event_wait_step = True
        func._wait_signal = f"event:{event_name}"
        func._wait_timeout = timeout
        
        # Convert method reference to name
        if on_timeout and callable(on_timeout):
            func._wait_on_timeout_step = on_timeout.__name__
        else:
            func._wait_on_timeout_step = on_timeout
        
        # Store display metadata on the function
        func._wait_display_title = display_title
        func._wait_display_subtitle = display_subtitle
        func._wait_display_waiting_for = display_waiting_for
        
        return func
    
    return decorator


def complete(display_title: Optional[str] = None, display_subtitle: Optional[str] = None, **result) -> Complete:
    return Complete(result=result if result else None, display_title=display_title, display_subtitle=display_subtitle)


def fail(reason: str) -> Fail:
    return Fail(reason)


def run_subflow(workflow_class: type, on_complete: Callable, on_create: Optional[Callable] = None, **context_kwargs) -> RunSubflow:
    """
    Run a subworkflow and resume at on_complete when it finishes.

    Args:
        workflow_class: The workflow class to run as a subworkflow
        on_complete: The step method to go to when subworkflow completes/fails
        on_create: Optional step method to call synchronously when subworkflow is created
        **context_kwargs: Arguments to pass to subworkflow's create_context

    Example:
        @step()
        def my_step(self):
            ctx = get_context()
            return run_subflow(
                SubWorkflow,
                on_complete=self.handle_result,
                on_create=self.setup_subflow,
                input_data=ctx.some_value
            )

        @step()
        def setup_subflow(self, child_run: WorkflowRun, parent_run: WorkflowRun):
            # Called synchronously when subworkflow is created, before it starts
            # Receives both the child and parent workflow runs
            pass

        @step()
        def handle_result(self, subflow_result: SubflowResult):
            ctx = get_context()
            # Access the child workflow's result
            if subflow_result.status == WorkflowStatus.COMPLETED:
                ctx.output = subflow_result.context.output_value
            else:
                # Handle failure
                ctx.error = subflow_result.error
            return complete()
    """
    on_complete_name = on_complete.__name__ if callable(on_complete) else str(on_complete)
    on_create_name = on_create.__name__ if callable(on_create) else None if on_create is None else str(on_create)
    return RunSubflow(
        workflow_class=workflow_class,
        on_complete=on_complete_name,
        context_kwargs=context_kwargs,
        on_create=on_create_name
    )


# Retry strategy
@dataclass
class Retry:
    max_attempts: int = 3
    base_delay: timedelta = timedelta(seconds=1)
    max_delay: timedelta = timedelta(minutes=5)
    backoff_factor: float = 2.0


# Context manager that handles persistence behind the scenes
class WorkflowContextManager:
    def __init__(self, run_id: int, context_class: type):
        self.run_id = run_id
        self.context_class = context_class
        self._run = None
        self._context = None
        self._original_data = None

    def get_context(self):
        """Return the actual Pydantic context model"""
        if self._context is None:
            if not self._run:
                self._run = WorkflowRun.objects.get(id=self.run_id)
            self._context = self.context_class.model_validate(self._run.data)
            self._original_data = dict(self._run.data)
        return self._context

    def commit_changes(self):
        """Save changes back to database using safe JSON serialization"""
        if self._context is not None and self._run:
            self._run.data = safe_model_dump(self._context)
            self._run.save()

    def rollback_changes(self):
        """Rollback any uncommitted changes"""
        if self._original_data is not None:
            self._context = None


# Thread-safe context storage
_current_context: ContextVar[Optional[WorkflowContextManager]] = ContextVar(
    "workflow_context", default=None
)


def get_context() -> BaseModel:
    """Get the current workflow context - returns the actual Pydantic model

    For better type hints, use: ctx = get_context()  # type: MyContextClass
    """
    ctx_manager = _current_context.get()
    if ctx_manager is None:
        raise RuntimeError(
            "No workflow context available - are you running inside a workflow step?"
        )
    return ctx_manager.get_context()


# Workflow registration
_workflows = {}
_event_workflows = {}  # workflows triggered by events


def workflow(name: str, version: str = "1", default_retry: Optional[Retry] = None):
    """Standard workflow decorator"""

    def decorator(cls):
        if not hasattr(cls, "Context"):
            raise ValueError(f"Workflow {name} must define a Context class")
        if not issubclass(cls.Context, BaseModel):
            raise ValueError(f"Workflow {name}.Context must inherit from BaseModel")

        # Auto-generate create_context if not provided
        if not hasattr(cls, "create_context"):

            @classmethod
            def create_context(cls_inner, **kwargs):
                return cls_inner.Context(**kwargs)

            cls.create_context = create_context

        start_step = None
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and getattr(attr, "_is_start_step", False):
                if start_step is not None:
                    raise ValueError(f"Workflow {name} has multiple start steps")
                start_step = attr_name

        if start_step is None:
            raise ValueError(
                f"Workflow {name} must have exactly one step marked with start=True"
            )

        # Find on_fail handler if defined
        on_fail_handler = None
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and getattr(attr, "_is_on_fail_handler", False):
                if on_fail_handler is not None:
                    raise ValueError(f"Workflow {name} has multiple @on_fail handlers")
                on_fail_handler = attr_name

        cls._workflow_name = name
        cls._workflow_version = version
        cls._default_retry = default_retry or Retry()
        cls._start_step = start_step
        cls._on_fail_handler = on_fail_handler

        _workflows[name] = cls
        return cls

    return decorator


def event_workflow(
    event_name: str,
    offset: OffsetType = None,
    version: str = "1",
    default_retry: Optional[Retry] = None,
    ignores_claims: bool = False,
):
    """
    Decorator for workflows triggered by events.
    Replaces the old automation system.

    Args:
        event_name: Name of the event that triggers this workflow
        offset: Offset from event time. Accepts timedelta or relativedelta.
               Negative = before, positive = after.
               Examples:
                 - timedelta(hours=-1): fires 1 hour BEFORE event time
                 - relativedelta(months=-1): fires 1 month BEFORE event time
        version: Workflow version string
        default_retry: Default retry policy for steps
        ignores_claims: If True, workflow runs regardless of event claims
    """

    def decorator(cls):
        nonlocal offset
        if offset is None:
            offset = timedelta()

        # Validate like normal workflow
        if not hasattr(cls, "Context"):
            raise ValueError(
                f"Event workflow for {event_name} must define a Context class"
            )
        if not issubclass(cls.Context, BaseModel):
            raise ValueError(f"Event workflow Context must inherit from BaseModel")

        # Auto-generate create_context if not provided - event workflows use same method name
        if not hasattr(cls, "create_context"):

            @classmethod
            def create_context(cls_inner, event=None, **kwargs):
                if event:
                    # Event workflow - extract useful fields from event
                    context_kwargs = {}
                    if hasattr(event, "entity_id"):
                        context_kwargs["entity_id"] = event.entity_id
                    if hasattr(event, "entity") and event.entity:
                        context_kwargs["entity_id"] = event.entity.id
                    return cls_inner.Context(**context_kwargs)
                else:
                    # Regular workflow - use kwargs
                    return cls_inner.Context(**kwargs)

            cls.create_context = create_context

        # Find start step
        start_step = None
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and getattr(attr, "_is_start_step", False):
                if start_step is not None:
                    raise ValueError(f"Event workflow has multiple start steps")
                start_step = attr_name

        if start_step is None:
            raise ValueError(f"Event workflow must have a start step")

        # Find on_fail handler if defined
        on_fail_handler = None
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and getattr(attr, "_is_on_fail_handler", False):
                if on_fail_handler is not None:
                    raise ValueError(f"Event workflow has multiple @on_fail handlers")
                on_fail_handler = attr_name

        # Generate unique workflow name
        workflow_name = f"event:{event_name}:{offset}"

        cls._workflow_name = workflow_name
        cls._workflow_version = version
        cls._default_retry = default_retry or Retry()
        cls._start_step = start_step
        cls._event_name = event_name
        cls._offset = offset
        cls._ignores_claims = ignores_claims
        cls._on_fail_handler = on_fail_handler

        # Register in both places
        _workflows[workflow_name] = cls

        # Group by event for efficient lookup
        if event_name not in _event_workflows:
            _event_workflows[event_name] = []
        _event_workflows[event_name].append(cls)

        return cls

    return decorator


def step(
    retry: Optional[Retry] = None,
    start: bool = False,
    visible: bool = True,
    title: Optional[str] = None
):
    """
    Decorator for workflow steps

    Args:
        retry: Retry policy for this step
        start: Whether this is the starting step
        visible: Whether this step should be visible in the frontend (default True)
        title: Optional display title for the frontend (defaults to function name if not set)
    """
    def decorator(func):
        func._is_workflow_step = True
        func._is_start_step = start
        func._step_retry = retry
        func._step_visible = visible
        func._step_title = title
        return func

    return decorator


def on_fail(func):
    """
    Decorator to mark a method as the failure handler for a workflow.

    The decorated method is called when the workflow fails (via Fail(),
    exception with max retries, etc.). It receives the WorkflowRun instance
    so it can inspect step history and perform compensation.

    Example:
        @workflow("order_processing")
        class OrderWorkflow:
            class Context(BaseModel):
                charge_id: Optional[str] = None
                reservation_id: Optional[str] = None

            @on_fail
            def cleanup(self, run: WorkflowRun):
                # Check which steps completed
                completed = set(
                    run.step_executions.filter(status='completed')
                    .values_list('step_name', flat=True)
                )

                ctx = get_context()
                if 'charge_payment' in completed and ctx.charge_id:
                    refund(ctx.charge_id)
                if 'reserve_inventory' in completed and ctx.reservation_id:
                    release_inventory(ctx.reservation_id)
    """
    func._is_on_fail_handler = True
    return func

# Main workflow engine
class WorkflowEngine:
    def __init__(self):
        self.executor = None

    def set_executor(self, executor):
        """Set the task executor"""
        self.executor = executor

    def start(self, workflow_name: str, **kwargs) -> WorkflowRun:
        """Start a new workflow manually"""
        if workflow_name not in _workflows:
            raise ValueError(f"Workflow {workflow_name} not found")

        workflow_cls = _workflows[workflow_name]
        initial_context = workflow_cls.create_context(**kwargs)
        data = safe_model_dump(initial_context)

        start_step_name = workflow_cls._start_step
        start_step_method = getattr(workflow_cls, start_step_name)

        # Determine initial status based on the start step's decorators
        initial_status = WorkflowStatus.RUNNING
        is_suspending_step = False
        if getattr(start_step_method, "_has_statezero_action", False):
            initial_status = WorkflowStatus.SUSPENDED
            is_suspending_step = True
        elif getattr(start_step_method, "_is_event_wait_step", False):
            initial_status = WorkflowStatus.SUSPENDED
            is_suspending_step = True

        run = WorkflowRun.objects.create(
            name=workflow_name,
            version=workflow_cls._workflow_version,
            current_step=start_step_name,
            data=data,
            status=initial_status,
        )

        # Only queue the step if the workflow is meant to be running
        if not is_suspending_step:
            self._queue_step(run.id, run.current_step)
        elif getattr(start_step_method, "_is_event_wait_step", False):
            # If it's a waiting step, we also need to set the signal info
            run.waiting_signal = getattr(start_step_method, "_wait_signal", "")
            run.on_timeout_step = (
                getattr(start_step_method, "_wait_on_timeout_step", "") or ""
            )
            timeout = getattr(start_step_method, "_wait_timeout", None)
            run.wake_at = timezone.now() + timeout if timeout else None
            run.waiting_display = {
                'display_title': getattr(start_step_method, "_wait_display_title", None),
                'display_subtitle': getattr(start_step_method, "_wait_display_subtitle", None),
                'display_waiting_for': getattr(start_step_method, "_wait_display_waiting_for", None)
            }
            run.save()

        return run

    def start_for_event(self, event, workflow_cls) -> WorkflowRun:
        """Start an event-triggered workflow"""
        # Create context from event - now uses unified method name
        initial_context = workflow_cls.create_context(event=event)
        data = safe_model_dump(initial_context)

        # Add event info to context
        data["_event_id"] = event.id
        data["_event_name"] = event.event_name
        data["_entity_id"] = event.entity_id

        run = WorkflowRun.objects.create(
            name=workflow_cls._workflow_name,
            version=workflow_cls._workflow_version,
            current_step=workflow_cls._start_step,
            data=data,
            status=WorkflowStatus.RUNNING,
            # Link to the event that triggered this
            triggered_by_event_id=event.id,
        )

        # Handle offset timing
        if workflow_cls._offset != timedelta():
            # Schedule for later based on offset
            if event.at:
                run_time = event.at + workflow_cls._offset
            else:
                run_time = timezone.now() + workflow_cls._offset

            if run_time > timezone.now():
                run.status = WorkflowStatus.WAITING
                run.wake_at = run_time
                run.save()
                return run

        # Run immediately
        self._queue_step(run.id, run.current_step)
        return run

    def handle_event_occurred(self, event):
        """Handle when an event occurs - start relevant workflows"""
        if event.event_name not in _event_workflows:
            return []

        workflows_started = []

        # Check claims once for all workflows
        from ..events.claims import find_matching_claim
        matching_claim = None
        try:
            entity = event.entity
            matching_claim = find_matching_claim(event.event_name, entity)
        except Exception:
            # If entity is deleted or claim check fails, continue without claim checking
            pass

        for workflow_cls in _event_workflows[event.event_name]:
            # Check if workflow should run for this event
            if hasattr(workflow_cls, "should_run_for_event"):
                if not workflow_cls.should_run_for_event(event):
                    continue

            # Check claims before starting workflow
            if matching_claim and not getattr(workflow_cls, "_ignores_claims", False):
                # There's a claim - only start if the workflow would be the holder
                # Event workflows can't be holders during startup, so skip
                # (the claiming workflow must already exist)
                continue

            try:
                run = self.start_for_event(event, workflow_cls)
                workflows_started.append(run)

                # If there's a matching claim, increment events_handled
                if matching_claim:
                    matching_claim.increment_events()
            except Exception as e:
                logger.error(
                    f"Failed to start workflow {workflow_cls._workflow_name} for event {event.id}: {e}",
                    exc_info=True,
                )

        return workflows_started

    def cancel(self, run_id: int):
        """Cancel a running workflow"""
        try:
            run = WorkflowRun.objects.get(id=run_id)
            if run.status in [
                WorkflowStatus.RUNNING,
                WorkflowStatus.WAITING,
                WorkflowStatus.SUSPENDED,
            ]:
                # Release all claims held by this workflow
                from ..events.claims import release_all_for_owner
                release_all_for_owner("workflow", run.id)

                run.status = WorkflowStatus.CANCELLED
                run.save()
        except WorkflowRun.DoesNotExist:
            pass

    def _queue_step(
        self, run_id: int, step_name: str, delay: Optional[timedelta] = None
    ):
        """Queue a step for execution"""
        if not self.executor:
            raise ValueError("No executor set")
        self.executor.queue_task("execute_step", run_id, step_name, delay=delay)

    def execute_step(self, run_id: int, step_name: str):
        """
        Execute a single workflow step atomically and safely, preventing race conditions.
        """
        ctx_manager = None
        token = None

        try:
            # This atomic block ensures all database operations within are "all or nothing."
            # If any error occurs, the entire transaction is rolled back automatically.
            with transaction.atomic():
                # .select_for_update() locks the database row. Any other process trying
                # to modify this run will wait until this transaction is complete.
                run = WorkflowRun.objects.select_for_update().get(id=run_id)

                # This status check is now 100% reliable due to the database lock.
                if run.status != WorkflowStatus.RUNNING:
                    return

                # --- Set up and execute the step ---
                workflow_cls = _workflows[run.name]
                ctx_manager = WorkflowContextManager(run_id, workflow_cls.Context)
                token = _current_context.set(ctx_manager)

                workflow_instance = workflow_cls()

                if not hasattr(workflow_instance, step_name):
                    raise ValueError(
                        f"Step {step_name} not found on {workflow_cls.__name__}"
                    )

                step_method = getattr(workflow_instance, step_name)
                if not getattr(step_method, "_is_workflow_step", False):
                    raise ValueError(f"Method {step_name} is not a workflow step")

                # Check if step accepts subflow_result parameter
                import inspect
                sig = inspect.signature(step_method)
                step_kwargs = {}

                if 'subflow_result' in sig.parameters and run.active_subworkflow_run_id:
                    # Load the child workflow result
                    child_run = WorkflowRun.objects.get(id=run.active_subworkflow_run_id)
                    child_workflow_cls = _workflows[child_run.name]
                    child_context = child_workflow_cls.Context.model_validate(child_run.data)

                    step_kwargs['subflow_result'] = SubflowResult(
                        status=child_run.status,
                        context=child_context,
                        run_id=child_run.id,
                        error=child_run.error
                    )

                # Execute step within claims context
                from ..events.claims import _context as claims_context
                with claims_context("workflow", workflow_cls, run.id):
                    result = step_method(**step_kwargs)

                if not isinstance(result, StepResult):
                    raise ValueError(
                        f"Step must return a StepResult, got {type(result)}"
                    )

                # Both the context data and the run's new status are saved here.
                # If either fails, the whole transaction rolls back.
                ctx_manager.commit_changes()
                self._handle_result(run, result)

        except WorkflowRun.DoesNotExist:
            # The run was deleted before we could process it. Nothing to do.
            return
        except Exception as e:
            # This block catches any exception from the transaction, including
            # database errors or errors from the step logic itself.
            if ctx_manager:
                ctx_manager.rollback_changes()  # Clear any in-memory changes.

            # The _handle_error method will run in its own, new transaction
            # to mark the workflow as failed.
            self._handle_error(run_id, step_name, e)
        finally:
            # This guarantees the thread-safe context is always reset,
            # preventing state from leaking between workflow runs.
            if token:
                _current_context.reset(token)

    def _call_on_fail_handler(self, run: WorkflowRun):
        """
        Call the workflow's @on_fail handler if one exists.

        The handler receives the WorkflowRun so it can inspect step history
        and perform compensation logic.
        """
        workflow_cls = _workflows.get(run.name)
        if not workflow_cls:
            return

        on_fail_handler_name = getattr(workflow_cls, "_on_fail_handler", None)
        if not on_fail_handler_name:
            return

        # Set up context so get_context() works inside the handler
        ctx_manager = WorkflowContextManager(run.id, workflow_cls.Context)
        token = _current_context.set(ctx_manager)

        try:
            workflow_instance = workflow_cls()
            on_fail_method = getattr(workflow_instance, on_fail_handler_name)

            # Execute within claims context
            from ..events.claims import _context as claims_context
            with claims_context("workflow", workflow_cls, run.id):
                on_fail_method(run)

            # Commit any context changes made by the handler
            ctx_manager.commit_changes()
        except Exception as e:
            # Log but don't fail - the workflow is already failing
            logger.error(
                f"Error in @on_fail handler {on_fail_handler_name} for workflow {run.name}: {e}",
                exc_info=True,
            )
        finally:
            _current_context.reset(token)

    def _handle_result(self, run: WorkflowRun, result: StepResult):
        """Handle step execution results"""
        run.refresh_from_db()
        workflow_cls = _workflows.get(run.name)

        if isinstance(result, Goto):

            StepExecution.objects.create(
                workflow_run=run,
                step_name=run.current_step,
                status='completed',
                step_display=_build_step_display(workflow_cls, run.current_step)
            )

            # Convert method reference to method name
            if callable(result.step):
                step_name = result.step.__name__
            else:
                step_name = str(result.step)

            target = getattr(workflow_cls, step_name, None)
            if not callable(target) or not getattr(target, "_is_workflow_step", False):
                raise ValueError(f"Target step {step_name} is not a @step")

            run.current_step = step_name

            if result.progress is not None:
                run.progress = max(0.0, min(100.0, result.progress))  # Clamp 0.0-100.0

            # --- Check if the target step requires suspension ---
            if getattr(target, "_has_statezero_action", False):
                run.status = WorkflowStatus.SUSPENDED
                run.save()
            elif getattr(target, "_is_event_wait_step", False):
                run.status = WorkflowStatus.SUSPENDED
                run.waiting_signal = getattr(target, "_wait_signal", "")
                run.on_timeout_step = getattr(target, "_wait_on_timeout_step", "") or ""
                timeout = getattr(target, "_wait_timeout", None)
                run.wake_at = timezone.now() + timeout if timeout else None
                run.waiting_display = {
                    'display_title': getattr(target, "_wait_display_title", None),
                    'display_subtitle': getattr(target, "_wait_display_subtitle", None),
                    'display_waiting_for': getattr(target, "_wait_display_waiting_for", None)
                }
                run.save()
            else:
                # It's a normal step, queue for immediate execution
                run.status = WorkflowStatus.RUNNING
                run.save()
                self._queue_step(run.id, step_name)

        elif isinstance(result, RunSubflow):
            # Mark current step as completed
            StepExecution.objects.create(
                workflow_run=run,
                step_name=run.current_step,
                status='completed',
                step_display=_build_step_display(workflow_cls, run.current_step)
            )

            # Validate the subworkflow class
            if not hasattr(result.workflow_class, '_workflow_name'):
                raise ValueError(f"{result.workflow_class.__name__} is not a registered @workflow")

            # Validate the on_complete step exists
            target = getattr(workflow_cls, result.on_complete, None)
            if not callable(target) or not getattr(target, "_is_workflow_step", False):
                raise ValueError(f"on_complete step {result.on_complete} is not a @step")

            # Validate the on_create step exists (if provided)
            if result.on_create:
                on_create_target = getattr(workflow_cls, result.on_create, None)
                if not callable(on_create_target) or not getattr(on_create_target, "_is_workflow_step", False):
                    raise ValueError(f"on_create step {result.on_create} is not a @step")

            # Create the child workflow
            child_context = result.workflow_class.create_context(**result.context_kwargs)
            child_data = safe_model_dump(child_context)

            child_run = WorkflowRun.objects.create(
                name=result.workflow_class._workflow_name,
                version=result.workflow_class._workflow_version,
                current_step=result.workflow_class._start_step,
                data=child_data,
                status=WorkflowStatus.RUNNING,
                parent_run_id=run.id,
            )

            # Call on_create callback if provided
            if result.on_create:
                # Set up context manager for the parent run so callback can modify context
                ctx_manager = WorkflowContextManager(run.id, workflow_cls.Context)
                token = _current_context.set(ctx_manager)

                try:
                    workflow_instance = workflow_cls()
                    on_create_method = getattr(workflow_instance, result.on_create)
                    on_create_method(child_run, run)

                    # Commit context changes made by the callback
                    ctx_manager.commit_changes()
                    # Reload run to get updated data
                    run.refresh_from_db()
                finally:
                    # Always reset the context
                    _current_context.reset(token)

            # Update parent context to store child run ID
            parent_context = workflow_cls.Context.model_validate(run.data)
            if hasattr(parent_context, 'active_subworkflow_run_id'):
                parent_context.active_subworkflow_run_id = child_run.id
                run.data = safe_model_dump(parent_context)

            # Update parent to track the child and suspend waiting for signal
            signal_name = f"subflow:{child_run.id}:complete"
            run.active_subworkflow_run_id = child_run.id
            run.current_step = result.on_complete
            run.status = WorkflowStatus.SUSPENDED
            run.waiting_signal = signal_name
            run.save()

            # Start the child workflow
            self._queue_step(child_run.id, child_run.current_step)

        elif isinstance(result, Sleep):
            run.status = WorkflowStatus.WAITING
            run.wake_at = timezone.now() + result.duration
            run.save()

        elif isinstance(result, Complete):
            StepExecution.objects.create(
                workflow_run=run,
                step_name=run.current_step,
                status='completed',
                step_display=_build_step_display(workflow_cls, run.current_step)
            )

            # Release all claims held by this workflow
            from ..events.claims import release_all_for_owner
            release_all_for_owner("workflow", run.id)

            run.status = WorkflowStatus.COMPLETED
            run.progress = 1.0
            if result.result:
                context = workflow_cls.Context.model_validate(run.data)
                for key, value in result.result.items():
                    if hasattr(context, key):
                        setattr(context, key, value)
                run.data = safe_model_dump(context)

            # Store completion display as structured JSON - used in the frontend only!
            run.completion_display = {
                'display_title': result.display_title,
                'display_subtitle': result.display_subtitle
            }

            # Clear waiting display - used in the frontend only!
            run.waiting_display = {}

            run.save()

            # If this is a subworkflow, signal the parent
            if run.parent_run_id:
                signal_name = f"subflow:{run.id}:complete"
                self.signal(signal_name)

        elif isinstance(result, Fail):
            StepExecution.objects.create(
                workflow_run=run,
                step_name=run.current_step,
                status='failed',
                error=result.reason,
                step_display=_build_step_display(workflow_cls, run.current_step)
            )

            # Call on_fail handler before cleanup (may modify context)
            self._call_on_fail_handler(run)

            # Refresh to get any context changes made by on_fail handler
            run.refresh_from_db()

            # Release all claims held by this workflow
            from ..events.claims import release_all_for_owner
            release_all_for_owner("workflow", run.id)

            run.status = WorkflowStatus.FAILED
            run.error = result.reason
            run.save()

            # If this is a subworkflow, signal the parent
            if run.parent_run_id:
                signal_name = f"subflow:{run.id}:complete"
                self.signal(signal_name)

    def signal(self, signal_name: str, payload: Optional[Dict[str, Any]] = None):
        """Send signal to waiting workflows"""
        waiting_runs = WorkflowRun.objects.filter(
            status=WorkflowStatus.SUSPENDED, waiting_signal=signal_name
        )

        for run in waiting_runs:
            if payload:
                workflow_cls = _workflows[run.name]
                context = workflow_cls.Context.model_validate(run.data)
                for key, value in payload.items():
                    if hasattr(context, key):
                        setattr(context, key, value)
                run.data = safe_model_dump(context)

            run.status = WorkflowStatus.RUNNING
            run.waiting_signal = ""
            run.wake_at = None
            run.save()

            self._queue_step(run.id, run.current_step)

    def _handle_error(self, run_id: int, step_name: str, error: Exception):
        """Handle step execution errors with retry"""
        
        # If WORKFLOW_TESTING_MODE is True, re-raise the original exception immediately
        if getattr(settings, 'WORKFLOW_TESTING_MODE', False):
            raise error
        
        try:
            run = WorkflowRun.objects.get(id=run_id)
        except WorkflowRun.DoesNotExist:
            return

        workflow_cls = _workflows[run.name]

        step_retry = None
        if hasattr(workflow_cls, step_name):
            step_method = getattr(workflow_cls, step_name)
            step_retry = getattr(step_method, "_step_retry", None)

        retry_policy = step_retry or workflow_cls._default_retry

        if run.retry_count < retry_policy.max_attempts - 1:
            run.retry_count += 1
            run.status = WorkflowStatus.WAITING

            n = run.retry_count - 1
            delay_secs = retry_policy.base_delay.total_seconds() * (
                retry_policy.backoff_factor**n
            )
            delay = timedelta(
                seconds=min(delay_secs, retry_policy.max_delay.total_seconds())
            )

            run.wake_at = timezone.now() + delay
            run.save()
            return

        # Max retries exhausted
        error_msg = f"{str(error)}\n{traceback.format_exc()}"
        if len(error_msg) > 32768:
            error_msg = error_msg[:32768] + "\n... (truncated)"

        StepExecution.objects.create(
            workflow_run=run,
            step_name=step_name,
            status='failed',
            error=error_msg,
            step_display=_build_step_display(workflow_cls, step_name)
        )

        # Call on_fail handler before cleanup (may modify context)
        self._call_on_fail_handler(run)

        # Refresh to get any context changes made by on_fail handler
        run.refresh_from_db()

        # Release all claims held by this workflow
        from ..events.claims import release_all_for_owner
        release_all_for_owner("workflow", run.id)

        run.error = error_msg
        run.status = WorkflowStatus.FAILED
        run.save()

    def process_scheduled(self):
        """Process workflows ready to wake up"""
        with transaction.atomic():
            now = timezone.now()
            # Find workflows that are either sleeping (WAITING) or have a signal timeout (SUSPENDED)
            ready_runs = WorkflowRun.objects.select_for_update(skip_locked=True).filter(
                Q(status=WorkflowStatus.WAITING, wake_at__lte=now)
                | Q(
                    status=WorkflowStatus.SUSPENDED,
                    wake_at__lte=now,
                    on_timeout_step__isnull=False,
                )
                & ~Q(on_timeout_step="")
            )

            for run in ready_runs:
                # If it's a timeout (must have an on_timeout_step)
                if run.on_timeout_step:
                    run.status = WorkflowStatus.RUNNING
                    run.current_step = run.on_timeout_step
                    run.waiting_signal = ""
                    run.on_timeout_step = ""
                    run.wake_at = None
                    run.save()
                    self._queue_step(run.id, run.current_step)
                # If it's a simple sleep (was in WAITING status)
                elif run.status == WorkflowStatus.WAITING:
                    run.status = WorkflowStatus.RUNNING
                    run.wake_at = None
                    run.save()
                    self._queue_step(run.id, run.current_step)


# Global engine
engine = WorkflowEngine()
