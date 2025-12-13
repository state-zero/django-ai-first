from datetime import timedelta
from typing import Optional, List, Union, Callable, Dict, Any
from dataclasses import dataclass, field
from pydantic import BaseModel
from django.conf import settings
from django.utils import timezone
from django.db import transaction

from ..workflows.core import (
    get_context,
    engine,
    Retry,
)
from ..workflows.models import WorkflowRun, WorkflowStatus

from .models import AgentRun, AgentStatus, HandlerExecution, HandlerStatus
from ..events.callbacks import on_event, OffsetType


def _render_template(template_str: str, context_dict: dict) -> str:
    """
    Render a Django template string against context.

    Context is available as 'ctx' in templates, e.g. "{{ ctx.booking_id }}".

    Args:
        template_str: Template like "{{ ctx.reservation_id }}" or "{{ ctx.priority|add:1 }}"
        context_dict: The agent's context dict

    Returns:
        Rendered string value
    """
    from django.template import Template, Context
    template = Template(template_str)
    return template.render(Context({"ctx": context_dict})).strip()


# Type for match dict values - template strings
MatchValue = str
MatchDict = Dict[str, MatchValue]


# Handler metadata stored on decorated methods
@dataclass
class HandlerInfo:
    """Metadata about a registered handler method"""
    method_name: str
    event_name: str
    offset: OffsetType = field(default_factory=timedelta)
    condition: Optional[Callable] = None
    retry: Optional[Retry] = None
    ignores_claims: bool = False
    match: Optional[MatchDict] = None


# Registry for agents and their handlers
_agents: Dict[str, type] = {}
_agent_handlers: Dict[str, List[HandlerInfo]] = {}  # agent_name -> list of handlers


def _get_model_for_event_type(event_type: str) -> Optional[type]:
    """
    Find the model class that defines this event type.
    """
    from django.apps import apps
    for model in apps.get_models():
        events = getattr(model, "events", None)
        if events:
            for event_def in events:
                if event_def.name == event_type:
                    return model
    return None


def _matches_context(match: MatchDict, entity, context_dict: dict) -> bool:
    """
    Check if an entity matches an agent's context based on match templates.

    Uses ORM query to validate the match, supporting Django's __ lookup syntax.

    Args:
        match: Dict of {entity_field: "{{ context_template }}"}
        entity: The event's entity instance
        context_dict: The agent's context as a dict

    Returns:
        True if all match conditions are satisfied
    """
    # Build ORM filter: {entity_field: rendered_context_value}
    filter_kwargs = {}
    for entity_field, template_str in match.items():
        rendered = _render_template(template_str, context_dict)
        # Convert to appropriate type if it looks like a number
        try:
            if rendered.isdigit():
                rendered = int(rendered)
        except (AttributeError, ValueError):
            pass
        filter_kwargs[entity_field] = rendered

    # Query: does this entity match the filter?
    model_class = entity.__class__
    try:
        return model_class.objects.filter(pk=entity.pk, **filter_kwargs).exists()
    except Exception:
        # Invalid filter - doesn't match
        return False


def _generate_namespace(match: MatchDict, context) -> str:
    """
    Generate a namespace string from match dict and context.

    Used for display and backward compatibility.
    """
    context_dict = context.model_dump() if hasattr(context, 'model_dump') else dict(context)

    parts = []
    for entity_field, template_str in sorted(match.items()):
        rendered = _render_template(template_str, context_dict)
        if rendered:
            parts.append(f"{entity_field}:{rendered}")

    return ",".join(parts) if parts else "*"


def _validate_match(event_type: str, match: MatchDict) -> None:
    """
    Validate that match dict references valid entity fields.

    Only runs in DEBUG mode to catch typos during development.
    Builds a test query to verify field names are valid.

    Args:
        event_type: Event name to find the entity model
        match: Dict of {entity_field: context_ref}

    Raises:
        ValueError: If entity field doesn't exist on the model
    """
    if not getattr(settings, 'DEBUG', False):
        return

    model = _get_model_for_event_type(event_type)
    if model is None:
        # Can't validate unknown event types
        return

    # Build a test filter to validate entity field paths
    entity_fields = list(match.keys())
    test_filter = {field: None for field in entity_fields}

    try:
        # Just build the query - don't execute it
        model.objects.filter(**test_filter).query
    except Exception as e:
        raise ValueError(
            f"Invalid match for event '{event_type}': {e}. "
            f"Check that fields {entity_fields} exist on {model.__name__}."
        ) from e


def handler(
    event_name: str,
    offset: OffsetType = None,
    condition: Optional[Callable[[Any, Any], bool]] = None,
    retry: Optional[Retry] = None,
    ignores_claims: bool = False,
    match: Optional[MatchDict] = None,
):
    """
    Decorator to mark a method as an event handler within an agent.

    Args:
        event_name: Event that triggers this handler
        offset: Offset from event time. Accepts timedelta or relativedelta.
               Negative = before, positive = after.
        condition: Optional callable(event, ctx) -> bool to conditionally execute handler
        retry: Retry policy for this handler
        ignores_claims: If True, handler runs regardless of event claims
        match: Dict mapping entity fields to context fields for routing.
               Format: {entity_field: ctx.context_field}
               Supports Django __ syntax for nested fields.

    Example:
        from django_ai.automation.agents import handler, ctx

        @handler("move_in", offset=timedelta(days=-3))  # 3 days before
        def send_pre_checkin_reminder(self, context):
            send_message(context.guest_name, "Your stay is coming up!")
            context.reminder_sent = True

        @handler("message_received", match={"sender_id": ctx.guest_id})
        def handle_message(self, context):
            # Routes messages where entity.sender_id == context.guest_id
            pass

        @handler("audit_event", ignores_claims=True)
        def audit_all(self, context):
            # Always runs, even when another flow claims this event
            pass
    """
    def decorator(func):
        # Validate match in debug mode
        if match:
            _validate_match(event_name, match)

        func._is_handler = True
        func._handler_event_name = event_name
        func._handler_offset = offset or timedelta()
        func._handler_condition = condition
        func._handler_retry = retry
        func._handler_ignores_claims = ignores_claims
        func._handler_match = match
        return func
    return decorator


def agent(
    name: str,
    spawn_on: str,
    match: Optional[MatchDict] = None,
    singleton: bool = True,
):
    """
    Agent decorator for handler-based agents.

    Args:
        name: Unique agent name (e.g., "reservation_journey")
        spawn_on: Event that creates a new agent instance (required)
        match: Dict for finding existing agent instances.
               Format: {entity_field: ctx.context_field}
               Supports Django __ syntax for nested fields.
               Used for singleton checks and event routing.
        singleton: If True (default), only one agent per context match.

    Required class members:
        - Context: Pydantic BaseModel defining agent state
        - create_context(cls, event) -> Context: Classmethod to create initial context

    Optional:
        - @handler methods: Event handlers with optional offsets and conditions

    Example:
        from django_ai.automation.agents import agent, handler, ctx

        @agent(
            "reservation_journey",
            spawn_on="reservation_created",
            match={"id": ctx.reservation_id}
        )
        class ReservationJourney:
            class Context(BaseModel):
                reservation_id: int
                guest_id: int
                reminder_sent: bool = False

            @classmethod
            def create_context(cls, event):
                reservation = event.entity
                return cls.Context(
                    reservation_id=reservation.id,
                    guest_id=reservation.guest_id,
                )

            @handler("move_in")
            def send_reminder(self, context):
                send_message(context.guest_name, "Check-in soon!")
                context.reminder_sent = True

            @handler("message_received", match={"sender_id": ctx.guest_id})
            def handle_message(self, context):
                # Different entity type - need explicit match to find agent
                pass
    """
    def decorator(cls):
        # Validate required members
        if not hasattr(cls, "Context"):
            raise ValueError(f"Agent {name} must define a Context class")
        if not issubclass(cls.Context, BaseModel):
            raise ValueError(f"Agent {name}.Context must inherit from BaseModel")
        if not hasattr(cls, "create_context"):
            raise ValueError(f"Agent {name} must have a 'create_context' classmethod")

        # Validate match in debug mode
        if match:
            _validate_match(spawn_on, match)

        # Store agent metadata
        cls._agent_name = name
        cls._spawn_on = spawn_on
        cls._singleton = singleton
        cls._default_match = match

        # Collect all handler methods
        handlers = []
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and getattr(attr, "_is_handler", False):
                handler_info = HandlerInfo(
                    method_name=attr_name,
                    event_name=attr._handler_event_name,
                    offset=attr._handler_offset,
                    condition=attr._handler_condition,
                    retry=attr._handler_retry,
                    ignores_claims=getattr(attr, "_handler_ignores_claims", False),
                    match=getattr(attr, "_handler_match", None),
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
            event_key = (handler_info.event_name, handler_info.offset)
            if event_key not in registered_events:
                _register_event_handler(name, handler_info, cls)
                registered_events.add(event_key)

        return cls

    return decorator


def _find_matching_agent(agent_name: str, match_spec: MatchDict, entity) -> Optional[AgentRun]:
    """
    Find an active agent that matches the given entity based on match templates.

    Args:
        agent_name: Name of the agent
        match_spec: Dict of {entity_field: "{{ context_template }}"}
        entity: The event's entity instance

    Returns:
        Matching AgentRun or None
    """
    # Get all active agents for this agent_name
    candidates = AgentRun.objects.filter(
        agent_name=agent_name,
        status=AgentStatus.ACTIVE,
    )

    # Post-filter by matching context
    for agent_run in candidates:
        if _matches_context(match_spec, entity, agent_run.context):
            return agent_run

    return None


def _register_spawn_handler(
    agent_name: str,
    event_name: str,
    singleton: bool,
    agent_class: type,
):
    """Register callback to spawn agent when spawn_on event occurs"""

    @on_event(event_name=event_name, namespace="*")
    def handle_spawn_event(event):
        # Create context first - we need it for singleton check and creation
        context = agent_class.create_context(event)
        context_data = context.model_dump()

        # Get the match spec (default match for spawn event)
        match_spec = agent_class._default_match

        # Check singleton constraint using match-based lookup
        if singleton and match_spec:
            existing = _find_matching_agent(agent_name, match_spec, event.entity)

            if existing:
                # Agent already exists for this context match, skip spawn
                return

        # Generate namespace for display/compatibility
        namespace = _generate_namespace(match_spec, context) if match_spec else "*"

        # Create new agent run
        agent_run = AgentRun.objects.create(
            agent_name=agent_name,
            namespace=namespace,
            entity_type_id=event.model_type_id,
            entity_id=str(event.entity_id),
            context=context_data,
            status=AgentStatus.ACTIVE,
        )

        # Schedule handlers that listen to the spawn event
        # These handlers missed the on_event callback since the agent
        # didn't exist yet when the event fired
        for handler_info in agent_class._handlers:
            if handler_info.event_name == event_name:
                agent_engine.schedule_handler(
                    agent_name=agent_name,
                    handler_name=handler_info.method_name,
                    event=event,
                    offset=handler_info.offset,
                )


def _register_event_handler(
    agent_name: str,
    handler_info: HandlerInfo,
    agent_class: type,
):
    """Register callback to execute handler when event occurs (with offset support).

    Uses @on_event with offset (timedelta), which:
    - For immediate events: fires when condition becomes true
    - For scheduled events: fires at (event.at + offset) time

    The event system handles offset-aware polling, so callbacks fire at the right time.
    """
    @on_event(
        event_name=handler_info.event_name,
        namespace="*",
        offset=handler_info.offset,
    )
    def handle_event_for_handler(event):
        # Schedule handler execution via the agent engine
        agent_engine.schedule_handler(
            agent_name=agent_name,
            handler_name=handler_info.method_name,
            event=event,
            offset=handler_info.offset,
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

        # Create context first - we need it for singleton check
        context = agent_class.create_context(event)
        context_data = context.model_dump()

        # Get match spec
        match_spec = agent_class._default_match

        # Check singleton constraint using match-based lookup
        if agent_class._singleton and match_spec:
            existing = _find_matching_agent(agent_name, match_spec, event.entity)

            if existing:
                return None

        # Generate namespace for display
        namespace = _generate_namespace(match_spec, context) if match_spec else "*"

        return AgentRun.objects.create(
            agent_name=agent_name,
            namespace=namespace,
            entity_type_id=event.model_type_id,
            entity_id=str(event.entity_id),
            context=context_data,
            status=AgentStatus.ACTIVE,
        )

    def schedule_handler(
        self,
        agent_name: str,
        handler_name: str,
        event,
        offset: OffsetType = None,
    ):
        """
        Schedule a handler for execution, respecting offset timing and claims.

        For non-zero offset:
            - Negative offset: Schedule before event.at time
            - Positive offset: Schedule after event.at time

        Offset can be timedelta or relativedelta for calendar-aware scheduling.

        If the scheduled time is in the past, executes immediately.

        Claim checking:
            - If event is claimed by another flow, handler is skipped
            - Unless handler has ignores_claims=True
        """
        if offset is None:
            offset = timedelta()

        if agent_name not in _agents:
            raise ValueError(f"Agent {agent_name} not found")

        agent_class = _agents[agent_name]

        # Find handler info first - we need it for match spec
        handler_info = None
        for h in agent_class._handlers:
            if h.method_name == handler_name:
                handler_info = h
                break

        # Get match spec: handler override > agent default
        match_spec = (handler_info.match if handler_info else None) or agent_class._default_match

        if not match_spec:
            # No match spec - can't route this event
            return None

        # Find matching agent using post-filtering
        agent_run = _find_matching_agent(agent_name, match_spec, event.entity)

        if not agent_run:
            # No agent exists for this match - skip handler
            return None

        # Check claims before scheduling
        from ..events.claims import find_matching_claim

        try:
            entity = event.entity
            matching_claim = find_matching_claim(event.event_name, entity)

            if matching_claim:
                # There's an active claim that matches this entity
                if handler_info and not handler_info.ignores_claims:
                    # Check if this agent run is the claim holder
                    is_holder = (
                        matching_claim.owner_type == "agent" and
                        matching_claim.owner_class == agent_class.__name__ and
                        matching_claim.owner_run_id == agent_run.id
                    )
                    if not is_holder:
                        # Skip - claimed by someone else
                        return None

                    # We are the holder - increment events_handled
                    matching_claim.increment_events()
        except Exception:
            # If entity is deleted or claim check fails, continue with scheduling
            pass

        # Calculate scheduled time based on event.at and offset
        if hasattr(event, 'at') and event.at:
            scheduled_at = event.at + offset
        else:
            # No event.at (immediate event) - schedule now + offset
            scheduled_at = timezone.now() + offset

        # Create handler execution record
        execution = HandlerExecution.objects.create(
            agent_run=agent_run,
            handler_name=handler_name,
            event_name=event.event_name,
            event_id=event.id,
            offset=offset,
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

                # Execute handler within claims context
                from ..events.claims import _context as claims_context
                print(f"[AGENT DEBUG] BEFORE handler {execution.handler_name}")
                try:
                    with claims_context("agent", agent_class, agent_run.id):
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
        """Mark an agent as completed and release all claims."""
        from ..events.claims import release_all_for_owner

        # Release all claims held by this agent
        release_all_for_owner("agent", agent_run.id)

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
            namespace: Optional explicit namespace (for display only)
            **context_kwargs: Additional context fields

        Returns:
            AgentRun if created, None if singleton prevented creation
        """
        if agent_name not in _agents:
            raise ValueError(f"Agent {agent_name} not found")

        agent_class = _agents[agent_name]

        # Create context
        if event:
            context = agent_class.create_context(event)
            # Overlay additional kwargs
            for key, value in context_kwargs.items():
                if hasattr(context, key):
                    setattr(context, key, value)
        else:
            context = agent_class.Context(**context_kwargs)

        context_data = context.model_dump()

        # Get match spec
        match_spec = agent_class._default_match

        # Check singleton constraint using match-based lookup
        if agent_class._singleton and match_spec and event:
            existing = _find_matching_agent(agent_name, match_spec, event.entity)

            if existing:
                return None

        # Generate namespace for display if not provided
        if namespace is None:
            namespace = _generate_namespace(match_spec, context) if match_spec else "*"

        return AgentRun.objects.create(
            agent_name=agent_name,
            namespace=namespace,
            entity_type_id=event.model_type_id if event else None,
            entity_id=str(event.entity_id) if event else "",
            context=context_data,
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
