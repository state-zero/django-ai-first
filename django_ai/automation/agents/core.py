from datetime import timedelta
from typing import Optional, List, Union, Callable, Dict, Any
from dataclasses import dataclass, field
from pydantic import BaseModel
from django.utils import timezone
from django.db import transaction

from ..workflows.core import (
    get_context,
    engine,
    Retry,
)
from ..workflows.models import WorkflowRun, WorkflowStatus

from .models import AgentRun, AgentStatus, HandlerExecution, HandlerStatus
from ..events.callbacks import on_event


# Handler metadata stored on decorated methods
@dataclass
class HandlerInfo:
    """Metadata about a registered handler method"""
    method_name: str
    event_name: str
    offset_minutes: int = 0
    condition: Optional[Callable] = None
    retry: Optional[Retry] = None


# Registry for agents and their handlers
_agents: Dict[str, type] = {}
_agent_handlers: Dict[str, List[HandlerInfo]] = {}  # agent_name -> list of handlers


def handler(
    event_name: str,
    offset_minutes: int = 0,
    condition: Optional[Callable[[Any, Any], bool]] = None,
    retry: Optional[Retry] = None,
):
    """
    Decorator to mark a method as an event handler within an agent.

    Args:
        event_name: Event that triggers this handler
        offset_minutes: Offset from event time in minutes. Negative = before, positive = after. Default 0.
        condition: Optional callable(event, ctx) -> bool to conditionally execute handler
        retry: Retry policy for this handler

    Example:
        @handler("move_in", offset_minutes=-3*24*60)  # -3 days before
        def send_pre_checkin_reminder(self, ctx: Context):
            send_message(ctx.guest_name, "Your stay is coming up!")
            ctx.reminder_sent = True
    """
    def decorator(func):
        func._is_handler = True
        func._handler_event_name = event_name
        func._handler_offset_minutes = offset_minutes
        func._handler_condition = condition
        func._handler_retry = retry
        return func
    return decorator


def agent(
    name: str,
    spawn_on: str,
    singleton: bool = True,
):
    """
    Agent decorator for handler-based agents.

    Args:
        name: Unique agent name (e.g., "reservation_journey")
        spawn_on: Event that creates a new agent instance (required)
        singleton: If True (default), only one agent per namespace. If another spawn_on
                   event fires for an existing namespace, no new agent is created.

    Required class members:
        - Context: Pydantic BaseModel defining agent state
        - get_namespace(self, event) -> str: Returns namespace string for scoping
        - create_context(cls, event) -> Context: Classmethod to create initial context

    Optional:
        - @handler methods: Event handlers with optional offsets and conditions

    Example:
        @agent("reservation_journey", spawn_on="reservation_created")
        class ReservationJourney:
            class Context(BaseModel):
                reservation_id: int
                guest_name: str
                reminder_sent: bool = False

            def get_namespace(self, event):
                return f"reservation:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                reservation = event.entity
                return cls.Context(
                    reservation_id=reservation.id,
                    guest_name=reservation.guest.name,
                )

            @handler("move_in", offset_minutes=-3*60)  # -3 hours
            def send_reminder(self, ctx: Context):
                send_message(ctx.guest_name, "Check-in soon!")
                ctx.reminder_sent = True
    """
    def decorator(cls):
        # Validate required members
        if not hasattr(cls, "Context"):
            raise ValueError(f"Agent {name} must define a Context class")
        if not issubclass(cls.Context, BaseModel):
            raise ValueError(f"Agent {name}.Context must inherit from BaseModel")
        if not hasattr(cls, "get_namespace"):
            raise ValueError(f"Agent {name} must have a 'get_namespace' method")
        if not hasattr(cls, "create_context"):
            raise ValueError(f"Agent {name} must have a 'create_context' classmethod")

        # Store agent metadata
        cls._agent_name = name
        cls._spawn_on = spawn_on
        cls._singleton = singleton

        # Collect all handler methods
        handlers = []
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and getattr(attr, "_is_handler", False):
                handler_info = HandlerInfo(
                    method_name=attr_name,
                    event_name=attr._handler_event_name,
                    offset_minutes=attr._handler_offset_minutes,
                    condition=attr._handler_condition,
                    retry=attr._handler_retry,
                )
                handlers.append(handler_info)

        cls._handlers = handlers

        # Register agent globally
        _agents[name] = cls
        _agent_handlers[name] = handlers

        # Register spawn event handler
        _register_spawn_handler(name, spawn_on, singleton, cls)

        # Register handlers for each unique event+offset combination
        registered_events = set()
        registered_events.add(spawn_on)  # spawn_on is already registered

        for handler_info in handlers:
            # Create unique key for event+offset
            event_key = (handler_info.event_name, handler_info.offset_minutes)
            if event_key not in registered_events:
                _register_event_handler(name, handler_info, cls)
                registered_events.add(event_key)

        return cls

    return decorator


def _register_spawn_handler(
    agent_name: str,
    event_name: str,
    singleton: bool,
    agent_class: type,
):
    """Register callback to spawn agent when spawn_on event occurs"""

    @on_event(event_name=event_name, namespace="*")
    def handle_spawn_event(event):
        # Compute namespace for this event
        temp_agent = agent_class()
        namespace = temp_agent.get_namespace(event)

        if namespace is None:
            namespace = "*"

        # Check singleton constraint
        if singleton:
            existing = AgentRun.objects.filter(
                agent_name=agent_name,
                namespace=namespace,
                status=AgentStatus.ACTIVE,
            ).first()

            if existing:
                # Agent already exists for this namespace, skip spawn
                return

        # Create new agent run
        context = agent_class.create_context(event)

        AgentRun.objects.create(
            agent_name=agent_name,
            namespace=namespace,
            entity_type_id=event.model_type_id,
            entity_id=str(event.entity_id),
            context=context.model_dump(),
            status=AgentStatus.ACTIVE,
        )


def _register_event_handler(
    agent_name: str,
    handler_info: HandlerInfo,
    agent_class: type,
):
    """Register callback to execute handler when event occurs at specified offset"""

    @on_event(event_name=handler_info.event_name, namespace="*")
    def handle_event_for_handler(event):
        # Schedule handler execution via the agent engine
        agent_engine.schedule_handler(
            agent_name=agent_name,
            handler_name=handler_info.method_name,
            event=event,
            offset_minutes=handler_info.offset_minutes,
        )


class AgentEngine:
    """Engine for managing agent lifecycle and handler execution"""

    def __init__(self):
        self.executor = None

    def set_executor(self, executor):
        """Set the task executor for async operations"""
        self.executor = executor

    def spawn(self, agent_name: str, event) -> Optional[AgentRun]:
        """
        Spawn a new agent instance from an event.

        Args:
            agent_name: Name of the registered agent
            event: Event that triggers the spawn

        Returns:
            AgentRun if created, None if singleton constraint prevented creation
        """
        if agent_name not in _agents:
            raise ValueError(f"Agent {agent_name} not found")

        agent_class = _agents[agent_name]
        temp_agent = agent_class()
        namespace = temp_agent.get_namespace(event)

        if namespace is None:
            namespace = "*"

        # Check singleton constraint
        if agent_class._singleton:
            existing = AgentRun.objects.filter(
                agent_name=agent_name,
                namespace=namespace,
                status=AgentStatus.ACTIVE,
            ).first()

            if existing:
                return None

        # Create new agent run
        context = agent_class.create_context(event)

        return AgentRun.objects.create(
            agent_name=agent_name,
            namespace=namespace,
            entity_type_id=event.model_type_id,
            entity_id=str(event.entity_id),
            context=context.model_dump(),
            status=AgentStatus.ACTIVE,
        )

    def schedule_handler(
        self,
        agent_name: str,
        handler_name: str,
        event,
        offset_minutes: int = 0,
    ):
        """
        Schedule a handler for execution, respecting offset timing.

        For offset_minutes != 0:
            - Negative offset: Schedule before event.at time
            - Positive offset: Schedule after event.at time

        If the scheduled time is in the past, executes immediately.
        """
        if agent_name not in _agents:
            raise ValueError(f"Agent {agent_name} not found")

        agent_class = _agents[agent_name]
        temp_agent = agent_class()
        namespace = temp_agent.get_namespace(event)

        if namespace is None:
            namespace = "*"

        # Look up existing agent for this namespace
        agent_run = AgentRun.objects.filter(
            agent_name=agent_name,
            namespace=namespace,
            status=AgentStatus.ACTIVE,
        ).first()

        if not agent_run:
            # No agent exists for this namespace - skip handler
            return None

        # Calculate scheduled time
        if offset_minutes != 0 and hasattr(event, 'at') and event.at:
            delay = timedelta(minutes=abs(offset_minutes))
            if offset_minutes < 0:
                scheduled_at = event.at - delay
            else:
                scheduled_at = event.at + delay
        else:
            scheduled_at = timezone.now()

        # Create handler execution record
        execution = HandlerExecution.objects.create(
            agent_run=agent_run,
            handler_name=handler_name,
            event_name=event.event_name,
            event_id=event.id,
            offset_minutes=offset_minutes,
            status=HandlerStatus.PENDING,
            scheduled_at=scheduled_at,
        )

        # If scheduled time is now or in the past, execute immediately
        if scheduled_at <= timezone.now():
            self._queue_handler_execution(execution.id)

        return execution

    def _queue_handler_execution(self, execution_id: int, delay: Optional[timedelta] = None):
        """Queue a handler execution for processing"""
        if self.executor:
            self.executor.queue_task("execute_handler", execution_id, delay=delay)
        else:
            # Synchronous execution for testing
            self.execute_handler(execution_id)

    def execute_handler(self, execution_id: int):
        """
        Execute a single handler atomically and safely.
        """
        try:
            with transaction.atomic():
                execution = HandlerExecution.objects.select_for_update().get(
                    id=execution_id
                )

                # Skip if not pending
                if execution.status != HandlerStatus.PENDING:
                    return

                agent_run = execution.agent_run

                # Skip if agent is not active
                if agent_run.status != AgentStatus.ACTIVE:
                    execution.status = HandlerStatus.SKIPPED
                    execution.error = "Agent not active"
                    execution.save()
                    return

                # Mark as running
                execution.status = HandlerStatus.RUNNING
                execution.started_at = timezone.now()
                execution.attempt_count += 1
                execution.save()

                # Get agent class and handler method
                agent_class = _agents[agent_run.agent_name]
                agent_instance = agent_class()
                handler_method = getattr(agent_instance, execution.handler_name)
                handler_info = None

                # Find handler info for retry policy
                for h in agent_class._handlers:
                    if h.method_name == execution.handler_name:
                        handler_info = h
                        break

                # Load context
                context = agent_class.Context.model_validate(agent_run.context)

                # Check condition if specified
                if handler_info and handler_info.condition:
                    event = None
                    if execution.event_id:
                        from ..events.models import Event
                        event = Event.objects.filter(id=execution.event_id).first()

                    if not handler_info.condition(event, context):
                        execution.status = HandlerStatus.SKIPPED
                        execution.completed_at = timezone.now()
                        execution.error = "Condition not met"
                        execution.save()
                        return

                # Execute handler
                try:
                    handler_method(context)

                    # Save updated context
                    agent_run.context = context.model_dump()
                    agent_run.updated_at = timezone.now()
                    agent_run.save()

                    # Mark as completed
                    execution.status = HandlerStatus.COMPLETED
                    execution.completed_at = timezone.now()
                    execution.save()

                except Exception as e:
                    # Handle error with retry logic
                    self._handle_handler_error(execution, handler_info, e)

        except HandlerExecution.DoesNotExist:
            return

    def _handle_handler_error(
        self,
        execution: HandlerExecution,
        handler_info: Optional[HandlerInfo],
        error: Exception,
    ):
        """Handle handler execution error with retry support"""
        import traceback

        error_msg = f"{str(error)}\n{traceback.format_exc()}"
        if len(error_msg) > 32768:
            error_msg = error_msg[:32768] + "\n... (truncated)"

        execution.error = error_msg

        # Check retry policy
        retry_policy = handler_info.retry if handler_info else None

        if retry_policy and execution.attempt_count < retry_policy.max_attempts:
            # Schedule retry
            n = execution.attempt_count - 1
            delay_secs = retry_policy.base_delay.total_seconds() * (
                retry_policy.backoff_factor ** n
            )
            delay = timedelta(
                seconds=min(delay_secs, retry_policy.max_delay.total_seconds())
            )

            execution.status = HandlerStatus.PENDING
            execution.scheduled_at = timezone.now() + delay
            execution.save()

            self._queue_handler_execution(execution.id, delay=delay)
        else:
            # Max retries exhausted or no retry policy
            execution.status = HandlerStatus.FAILED
            execution.completed_at = timezone.now()
            execution.save()

    def get_agent_for_namespace(
        self,
        agent_name: str,
        namespace: str,
    ) -> Optional[AgentRun]:
        """Get existing agent for namespace, or None."""
        return AgentRun.objects.filter(
            agent_name=agent_name,
            namespace=namespace,
            status=AgentStatus.ACTIVE,
        ).first()

    def list_handlers(self, agent_name: str) -> List[HandlerInfo]:
        """List all handlers registered for an agent."""
        return _agent_handlers.get(agent_name, [])

    def complete_agent(self, agent_run: AgentRun) -> None:
        """Mark an agent as completed."""
        agent_run.status = AgentStatus.COMPLETED
        agent_run.save()

    def process_scheduled_handlers(self):
        """Process handlers that are ready to execute (scheduled_at <= now)"""
        now = timezone.now()

        ready_executions = HandlerExecution.objects.filter(
            status=HandlerStatus.PENDING,
            scheduled_at__lte=now,
        ).select_for_update(skip_locked=True)

        for execution in ready_executions:
            self._queue_handler_execution(execution.id)


# Global agent engine
agent_engine = AgentEngine()


# Backward compatibility: AgentManager for manual operations
class AgentManager:
    @staticmethod
    def spawn(
        agent_name: str,
        event=None,
        namespace: Optional[str] = None,
        **context_kwargs,
    ) -> Optional[AgentRun]:
        """
        Manually spawn an agent.

        Args:
            agent_name: Name of the registered agent
            event: Optional event to initialize from
            namespace: Optional explicit namespace (overrides get_namespace)
            **context_kwargs: Additional context fields

        Returns:
            AgentRun if created, None if singleton prevented creation
        """
        if agent_name not in _agents:
            raise ValueError(f"Agent {agent_name} not found")

        agent_class = _agents[agent_name]

        # Determine namespace
        if namespace is None and event:
            temp_agent = agent_class()
            namespace = temp_agent.get_namespace(event)
        if namespace is None:
            namespace = "*"

        # Check singleton constraint
        if agent_class._singleton:
            existing = AgentRun.objects.filter(
                agent_name=agent_name,
                namespace=namespace,
                status=AgentStatus.ACTIVE,
            ).first()

            if existing:
                return None

        # Create context
        if event:
            context = agent_class.create_context(event)
            # Overlay additional kwargs
            for key, value in context_kwargs.items():
                if hasattr(context, key):
                    setattr(context, key, value)
        else:
            context = agent_class.Context(**context_kwargs)

        return AgentRun.objects.create(
            agent_name=agent_name,
            namespace=namespace,
            entity_type_id=None,
            entity_id="",
            context=context.model_dump(),
            status=AgentStatus.ACTIVE,
        )

    @staticmethod
    def complete(agent_run: AgentRun):
        """Mark an agent as completed"""
        agent_engine.complete_agent(agent_run)

    @staticmethod
    def list_active(
        agent_name: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> List[AgentRun]:
        """List active agents, optionally filtered by name and namespace"""
        qs = AgentRun.objects.filter(status=AgentStatus.ACTIVE)

        if agent_name:
            qs = qs.filter(agent_name=agent_name)
        if namespace:
            qs = qs.filter(namespace=namespace)

        return list(qs)
