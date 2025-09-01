import logging
import traceback
from datetime import timedelta
from typing import Dict, Any, Optional, Callable, TypeVar, Generic
from pydantic import BaseModel

# Type variable for workflow context
T = TypeVar("T", bound=BaseModel)
from dataclasses import dataclass
from pydantic import BaseModel
from contextvars import ContextVar
from django.db import transaction
from .models import WorkflowRun, WorkflowStatus
from django.utils import timezone

logger = logging.getLogger(__name__)


# Return types for step functions
@dataclass
class StepResult:
    pass


@dataclass
class Goto(StepResult):
    step: str


@dataclass
class Sleep(StepResult):
    duration: timedelta


@dataclass
class Wait(StepResult):
    signal: str
    timeout: Optional[timedelta] = None
    on_timeout: Optional[str] = None


@dataclass
class Complete(StepResult):
    result: Optional[Dict[str, Any]] = None


@dataclass
class Fail(StepResult):
    reason: str


# Helper functions
def goto(step: str) -> Goto:
    return Goto(step)


def sleep(duration: timedelta) -> Sleep:
    return Sleep(duration)


def wait(
    signal: str, timeout: Optional[timedelta] = None, on_timeout: Optional[str] = None
) -> Wait:
    """Wait for a specific signal"""
    return Wait(signal, timeout, on_timeout)


def wait_for_event(
    event_name: str,
    timeout: Optional[timedelta] = None,
    on_timeout: Optional[str] = None,
) -> Wait:
    """Wait for a specific event to occur"""
    signal_name = f"event:{event_name}"
    return Wait(signal_name, timeout, on_timeout)


# More intuitive wait with dot notation
class WaitBuilder:
    @staticmethod
    def for_signal(
        signal: str,
        timeout: Optional[timedelta] = None,
        on_timeout: Optional[str] = None,
    ) -> Wait:
        """Wait for a specific signal"""
        return Wait(signal, timeout, on_timeout)

    @staticmethod
    def for_event(
        event_name: str,
        timeout: Optional[timedelta] = None,
        on_timeout: Optional[str] = None,
    ) -> Wait:
        """Wait for a specific event to occur"""
        signal_name = f"event:{event_name}"
        return Wait(signal_name, timeout, on_timeout)


# Create instance for dot notation
wait = WaitBuilder()


def complete(**result) -> Complete:
    return Complete(result)


def fail(reason: str) -> Fail:
    return Fail(reason)


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
        """Save changes back to database"""
        if self._context is not None and self._run:
            self._run.data = self._context.model_dump()
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

        cls._workflow_name = name
        cls._workflow_version = version
        cls._default_retry = default_retry or Retry()
        cls._start_step = start_step

        _workflows[name] = cls
        return cls

    return decorator


def event_workflow(
    event_name: str,
    entity_type: str = "*",
    offset_minutes: int = 0,
    version: str = "1",
    default_retry: Optional[Retry] = None,
):
    """
    Decorator for workflows triggered by events.
    Replaces the old automation system.
    """

    def decorator(cls):
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

        # Generate unique workflow name
        workflow_name = f"event:{event_name}:{entity_type}:{offset_minutes}"

        cls._workflow_name = workflow_name
        cls._workflow_version = version
        cls._default_retry = default_retry or Retry()
        cls._start_step = start_step
        cls._event_name = event_name
        cls._entity_type = entity_type
        cls._offset_minutes = offset_minutes

        # Register in both places
        _workflows[workflow_name] = cls

        # Group by event for efficient lookup
        if event_name not in _event_workflows:
            _event_workflows[event_name] = []
        _event_workflows[event_name].append(cls)

        return cls

    return decorator


def step(retry: Optional[Retry] = None, start: bool = False):
    """Decorator for workflow steps"""

    def decorator(func):
        func._is_workflow_step = True
        func._is_start_step = start
        func._step_retry = retry
        return func

    return decorator


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
        data = initial_context.model_dump()

        run = WorkflowRun.objects.create(
            name=workflow_name,
            version=workflow_cls._workflow_version,
            current_step=workflow_cls._start_step,
            data=data,
            status=WorkflowStatus.RUNNING,
        )

        self._queue_step(run.id, run.current_step)
        return run

    def start_for_event(self, event, workflow_cls) -> WorkflowRun:
        """Start an event-triggered workflow"""
        # Create context from event - now uses unified method name
        initial_context = workflow_cls.create_context(event=event)
        data = initial_context.model_dump()

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
        if workflow_cls._offset_minutes != 0:
            # Schedule for later
            delay = timedelta(minutes=abs(workflow_cls._offset_minutes))
            if workflow_cls._offset_minutes < 0:
                # Negative offset - schedule before event time
                run_time = event.at - delay if event.at else timezone.now()
            else:
                # Positive offset - schedule after event time
                run_time = (event.at + delay) if event.at else (timezone.now() + delay)

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

        for workflow_cls in _event_workflows[event.event_name]:
            # Check entity type filter
            if (
                workflow_cls._entity_type != "*"
                and workflow_cls._entity_type != event.model_type.model
            ):
                continue

            # Check if workflow should run for this event
            if hasattr(workflow_cls, "should_run_for_event"):
                if not workflow_cls.should_run_for_event(event):
                    continue

            try:
                run = self.start_for_event(event, workflow_cls)
                workflows_started.append(run)
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
            if run.status in [WorkflowStatus.RUNNING, WorkflowStatus.WAITING]:
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

                result = step_method()

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

    def _handle_result(self, run: WorkflowRun, result: StepResult):
        """Handle step execution results"""
        run.refresh_from_db()

        if isinstance(result, Goto):
            workflow_cls = _workflows[run.name]

            # Convert method reference to method name
            if callable(result.step):
                step_name = result.step.__name__
            else:
                # Fallback for backward compatibility
                step_name = str(result.step)

            target = getattr(workflow_cls, step_name, None)
            if not callable(target) or not getattr(target, "_is_workflow_step", False):
                raise ValueError(f"Target step {step_name} is not a @step")

            run.current_step = step_name
            run.save()
            self._queue_step(run.id, step_name)

        elif isinstance(result, Sleep):
            run.status = WorkflowStatus.WAITING
            run.wake_at = timezone.now() + result.duration
            run.save()

        elif isinstance(result, Wait):
            run.status = WorkflowStatus.WAITING
            run.waiting_signal = result.signal

            # Convert method reference to method name for timeout
            if result.on_timeout and callable(result.on_timeout):
                timeout_step_name = result.on_timeout.__name__
            else:
                timeout_step_name = str(result.on_timeout) if result.on_timeout else ""

            run.on_timeout_step = timeout_step_name
            run.wake_at = timezone.now() + result.timeout if result.timeout else None
            run.save()

        elif isinstance(result, Complete):
            run.status = WorkflowStatus.COMPLETED
            if result.result:
                workflow_cls = _workflows[run.name]
                context = workflow_cls.Context.model_validate(run.data)
                for key, value in result.result.items():
                    if hasattr(context, key):
                        setattr(context, key, value)
                run.data = context.model_dump()
            run.save()

        elif isinstance(result, Fail):
            run.status = WorkflowStatus.FAILED
            run.error = result.reason
            run.save()

    def signal(self, signal_name: str, payload: Optional[Dict[str, Any]] = None):
        """Send signal to waiting workflows"""
        waiting_runs = WorkflowRun.objects.filter(
            status=WorkflowStatus.WAITING, waiting_signal=signal_name
        )

        for run in waiting_runs:
            if payload:
                workflow_cls = _workflows[run.name]
                context = workflow_cls.Context.model_validate(run.data)
                for key, value in payload.items():
                    if hasattr(context, key):
                        setattr(context, key, value)
                run.data = context.model_dump()

            run.status = WorkflowStatus.RUNNING
            run.waiting_signal = ""
            run.wake_at = None
            run.save()

            self._queue_step(run.id, run.current_step)

    def _handle_error(self, run_id: int, step_name: str, error: Exception):
        """Handle step execution errors with retry"""
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

        run.error = error_msg
        run.status = WorkflowStatus.FAILED
        run.save()

    def process_scheduled(self):
        """Process workflows ready to wake up"""
        with transaction.atomic():
            ready_runs = WorkflowRun.objects.select_for_update(skip_locked=True).filter(
                status=WorkflowStatus.WAITING, wake_at__lte=timezone.now()
            )

            for run in ready_runs:
                if run.waiting_signal and run.on_timeout_step:
                    run.status = WorkflowStatus.RUNNING
                    run.current_step = run.on_timeout_step
                    run.waiting_signal = ""
                    run.on_timeout_step = ""
                    run.wake_at = None
                    run.save()
                    self._queue_step(run.id, run.current_step)
                else:
                    run.status = WorkflowStatus.RUNNING
                    run.wake_at = None
                    run.save()
                    self._queue_step(run.id, run.current_step)


# Global engine
engine = WorkflowEngine()