from datetime import timedelta
from typing import Optional, List, Union
from pydantic import BaseModel
from django.utils import timezone
from ..workflows.core import (
    event_workflow,
    workflow,
    step,
    get_context,
    sleep,
    wait_for_event,
    complete,
    goto,
    engine,
)

from .models import AgentRun, AgentStatus
from ..events.callbacks import on_event


def agent(
    spawn_on: List[str],
    act_on: Optional[List[str]] = None,
    heartbeat: Optional[timedelta] = None,
    singleton: bool = False,
):
    """
    Agent decorator.

    Args:
        spawn_on: List of event names that spawn this agent
        act_on: List of event names that wake up existing agent to act again
        heartbeat: Optional periodic heartbeat (agent wakes up every N time)
        singleton: If True, only one instance runs at a time per namespace
    """

    def decorator(cls):
        # Validate required methods
        if not hasattr(cls, "act"):
            raise ValueError(f"Agent must have an 'act' method")
        if not hasattr(cls, "get_namespace"):
            raise ValueError(f"Agent must have a 'get_namespace' method")

        # Add agent context fields
        if hasattr(cls, "Context"):
            original_context = cls.Context
        else:
            original_context = type("Context", (BaseModel,), {})

        class AgentContext(original_context):
            triggering_event_id: Optional[int] = None
            current_event_id: Optional[int] = None  # Event that woke us this time
            current_event_name: Optional[str] = None
            agent_namespaces: List[str] = []  # List of namespaces this agent listens to
            spawned_at: str = ""
            last_action: str = ""
            _should_finish: bool = False

        cls.Context = AgentContext

        # Add finish() method to agent instances
        def finish(self):
            """Mark this agent to finish after current act() completes"""
            ctx = get_context()
            ctx._should_finish = True

        cls.finish = finish

        # Convert to workflow (kept as-is)
        @workflow(f"agent:{cls.__name__.lower()}")
        class AgentWorkflow:
            Context = AgentContext

            @classmethod
            def create_context(cls, event=None, **kwargs):
                ctx_data = kwargs
                if event:
                    # Create temporary agent instance to compute namespace(s)
                    temp_agent = cls()
                    agent_namespaces = temp_agent.get_namespace(event)

                    # Normalize to list
                    if isinstance(agent_namespaces, str):
                        agent_namespaces = [agent_namespaces]

                    ctx_data.update(
                        {
                            "triggering_event_id": event.id,
                            "current_event_id": event.id,
                            "current_event_name": event.event_name,
                            "agent_namespaces": agent_namespaces,
                            "spawned_at": timezone.now().isoformat(),
                        }
                    )
                return AgentContext(**ctx_data)

            @step(start=True)
            def _run_agent(self):
                # Create the actual agent instance and run it
                agent_instance = cls()
                return self._agent_loop(agent_instance)

            @step()
            def _agent_loop(self, agent_instance):
                ctx = get_context()

                if ctx._should_finish:
                    return complete(reason="agent_finished")

                try:
                    # Call the agent's act method
                    agent_instance.act()
                    ctx.last_action = timezone.now().isoformat()

                    # Check if agent called finish() during act()
                    if ctx._should_finish:
                        return complete(reason="agent_finished")

                    # Decide what to do next based on strategy
                    if heartbeat and act_on:
                        # Hybrid: wait for act events OR heartbeat timeout
                        return wait_for_event(
                            (
                                act_on[0] if len(act_on) == 1 else act_on
                            ),  # Support multiple events
                            timeout=heartbeat,
                            on_timeout=lambda: self._agent_loop(agent_instance),
                        )
                    elif act_on:
                        # Event-driven: wait for act events indefinitely
                        return wait_for_event(act_on[0] if len(act_on) == 1 else act_on)
                    elif heartbeat:
                        # Heartbeat-only: sleep then continue
                        return sleep(heartbeat).then(
                            lambda: self._agent_loop(agent_instance)
                        )
                    else:
                        # No strategy - agent runs once then finishes
                        return complete(reason="single_run_complete")

                except Exception as e:
                    return complete(reason=f"agent_error: {str(e)}")

        # Register event spawners with namespace filtering
        for event_name in spawn_on:
            _register_spawner(
                cls.__name__.lower(),
                event_name,
                singleton,
                is_spawn_event=True,
                agent_class=cls,
            )

        # Register act event handlers with namespace filtering
        if act_on:
            for event_name in act_on:
                _register_spawner(
                    cls.__name__.lower(),
                    event_name,
                    singleton,
                    is_spawn_event=False,
                    agent_class=cls,
                )

        return cls

    return decorator


def _register_spawner(
    agent_name: str,
    event_name: str,
    singleton: bool,
    is_spawn_event: bool = True,
    agent_class=None,
):
    """Register callback to spawn agent when event occurs"""

    @on_event(event_name=event_name, namespace="*")  # Listen to all namespaces
    def handle_agent_event(event):
        workflow_name = f"agent:{agent_name}"

        # Compute namespace(s) for this event using agent's get_namespace method
        if agent_class:
            temp_agent = agent_class()
            event_namespaces = temp_agent.get_namespace(event)
            # Normalize to list
            if isinstance(event_namespaces, str):
                event_namespaces = [event_namespaces]
        else:
            event_namespaces = ["*"]

        # Check if this event's namespace matches any agent namespaces
        if not _event_matches_agent_namespaces(event, event_namespaces):
            return

        if is_spawn_event:
            # Handle singleton constraint for spawn events
            if singleton:
                # Look for existing agents that have overlapping namespaces
                existing = _find_agent_in_namespaces(agent_name, event_namespaces)

                if existing:
                    # Wake existing agent with new event data
                    _wake_agent_with_event(existing, event)
                    return

            # Spawn new agent via workflow engine (unchanged)
            engine.start(workflow_name, event=event)

        else:
            # This is an act event - wake existing agents with matching namespaces
            existing_agents = _find_agents_in_namespaces(agent_name, event_namespaces)

            for agent_run in existing_agents:
                _wake_agent_with_event(agent_run, event)


def _event_matches_agent_namespaces(event, agent_namespaces: List[str]) -> bool:
    """Check if event's namespace matches any of the agent's namespaces"""
    event_namespace = getattr(event, "namespace", "*")

    for agent_ns in agent_namespaces:
        if agent_ns == "*" or event_namespace == "*" or agent_ns == event_namespace:
            return True
    return False


# ⬇️ These functions now query AgentRun and its fields
def _find_agent_in_namespaces(agent_name: str, namespaces: List[str]):
    """Find a single existing agent that operates in any of these namespaces"""
    for run in AgentRun.objects.filter(
        agent_name=agent_name, status__in=[AgentStatus.RUNNING, AgentStatus.WAITING]
    ):
        agent_namespaces = run.namespaces or []
        # Check for overlap
        if any(ns in agent_namespaces for ns in namespaces):
            return run
    return None


def _find_agents_in_namespaces(agent_name: str, namespaces: List[str]):
    """Find all existing agents that operate in any of these namespaces"""
    matching_agents = []
    for run in AgentRun.objects.filter(
        agent_name=agent_name, status__in=[AgentStatus.RUNNING, AgentStatus.WAITING]
    ):
        agent_namespaces = run.namespaces or []
        if any(ns in agent_namespaces for ns in namespaces):
            matching_agents.append(run)
    return matching_agents


def _wake_agent_with_event(agent_run: AgentRun, event):
    """Wake an existing agent with new event data"""
    # Update agent context with new event info
    ctx = agent_run.context_data or {}
    ctx["current_event_id"] = event.id
    ctx["current_event_name"] = event.event_name
    agent_run.context_data = ctx
    agent_run.current_event_id = event.id
    agent_run.current_event_name = event.event_name
    agent_run.save(
        update_fields=[
            "context_data",
            "current_event_id",
            "current_event_name",
            "updated_at",
        ]
    )

    # Send signal to wake the backing workflow
    engine.signal(
        f"event:{event.event_name}",
        {
            "event_id": event.id,
            "entity_id": event.entity_id,
            "event_name": event.event_name,
        },
    )


# Helper to manage agents
class AgentManager:
    @staticmethod
    def spawn(
        agent_class_name: str,
        namespaces: Optional[Union[str, List[str]]] = None,
        **context_kwargs,
    ):
        """Manually spawn an agent in specific namespace(s)"""
        workflow_name = f"agent:{agent_class_name.lower()}"
        if namespaces:
            if isinstance(namespaces, str):
                namespaces = [namespaces]
            context_kwargs["agent_namespaces"] = namespaces
        return engine.start(workflow_name, **context_kwargs)

    @staticmethod
    def finish_all(agent_class_name: str, namespace: Optional[str] = None):
        """Mark all instances of an agent type to finish, optionally filtered by namespace"""
        # Query AgentRun, not WorkflowRun
        active_runs = AgentRun.objects.filter(
            agent_name=agent_class_name.lower(),
            status__in=[AgentStatus.RUNNING, AgentStatus.WAITING],
        )

        if namespace:
            active_runs = [
                r
                for r in active_runs
                if (r.namespaces or []) and namespace in r.namespaces
            ]

        for run in active_runs:
            ctx = run.context_data or {}
            ctx["_should_finish"] = True
            run.context_data = ctx
            run.save(update_fields=["context_data", "updated_at"])

    @staticmethod
    def list_active(
        agent_class_name: Optional[str] = None, namespace: Optional[str] = None
    ):
        """List active agents, optionally filtered by type and namespace"""
        filter_kwargs = {"status__in": [AgentStatus.RUNNING, AgentStatus.WAITING]}

        if agent_class_name:
            filter_kwargs["agent_name"] = agent_class_name.lower()

        active_runs = AgentRun.objects.filter(**filter_kwargs)

        if namespace:
            return [
                r
                for r in active_runs
                if (r.namespaces or []) and namespace in r.namespaces
            ]

        return list(active_runs)
