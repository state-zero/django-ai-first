from django.db import models
from django.utils import timezone


class AgentStatus(models.TextChoices):
    RUNNING = "running", "Running"
    WAITING = "waiting", "Waiting"
    FINISHED = "finished", "Finished"
    FAILED = "failed", "Failed"


class AgentRun(models.Model):
    """A running agent instance"""

    # Agent identity
    agent_name = models.CharField(max_length=100)  # e.g. "useronboardingagent"
    agent_version = models.CharField(max_length=20, default="1")

    # Context and state
    context_data = models.JSONField(default=dict)  # Agent's Context data
    namespaces = models.JSONField(
        default=list
    )  # List of namespaces this agent listens to

    # Current state
    status = models.CharField(
        max_length=20, choices=AgentStatus.choices, default=AgentStatus.RUNNING
    )

    # Event tracking
    triggering_event_id = models.IntegerField(
        null=True, blank=True
    )  # Event that spawned this agent
    current_event_id = models.IntegerField(
        null=True, blank=True
    )  # Event that last woke this agent
    current_event_name = models.CharField(
        max_length=100, blank=True
    )  # Name of current event

    # Timing
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    wake_at = models.DateTimeField(null=True, blank=True)  # For heartbeat scheduling
    last_action_at = models.DateTimeField(null=True, blank=True)

    # Error handling
    error_message = models.TextField(blank=True)
    retry_count = models.IntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["agent_name", "status"]),
            models.Index(fields=["status", "wake_at"]),
            models.Index(fields=["triggering_event_id"]),
            # JSON field indexes for namespace queries
            models.Index(fields=["agent_name"], name="agent_name_idx"),
        ]

    def __str__(self):
        return f"{self.agent_name}#{self.id} ({self.status})"

    def is_in_namespace(self, namespace: str) -> bool:
        """Check if this agent operates in the given namespace"""
        return namespace in self.namespaces

    def mark_finished(self, reason: str = ""):
        """Mark this agent as finished"""
        self.status = AgentStatus.FINISHED
        if reason:
            self.error_message = reason
        self.save()

    def mark_failed(self, error: str):
        """Mark this agent as failed"""
        self.status = AgentStatus.FAILED
        self.error_message = error
        self.save()

    def update_current_event(self, event):
        """Update the current event that woke this agent"""
        self.current_event_id = event.id
        self.current_event_name = event.event_name
        self.updated_at = timezone.now()
        self.save(
            update_fields=["current_event_id", "current_event_name", "updated_at"]
        )


class AgentExecutionLog(models.Model):
    """Log of agent executions for debugging/monitoring"""

    agent_run = models.ForeignKey(
        AgentRun, on_delete=models.CASCADE, related_name="executions"
    )

    # Execution details
    event_id = models.IntegerField(
        null=True, blank=True
    )  # Event that triggered this execution
    event_name = models.CharField(max_length=100, blank=True)
    execution_type = models.CharField(max_length=20)  # "spawn", "act", "heartbeat"

    # Results
    success = models.BooleanField(default=True)
    error_message = models.TextField(blank=True)
    execution_time_ms = models.IntegerField(null=True, blank=True)

    # Context
    context_before = models.JSONField(
        null=True, blank=True
    )  # Context state before execution
    context_after = models.JSONField(
        null=True, blank=True
    )  # Context state after execution

    # Timing
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["agent_run", "-started_at"]),
            models.Index(fields=["success", "execution_type"]),
        ]

    def __str__(self):
        return (
            f"{self.agent_run.agent_name} - {self.execution_type} ({self.started_at})"
        )


# Manager for agent operations
class AgentRunManager(models.Manager):

    def active_agents(self, agent_name=None, namespace=None):
        """Get active agent runs"""
        qs = self.filter(status__in=[AgentStatus.RUNNING, AgentStatus.WAITING])

        if agent_name:
            qs = qs.filter(agent_name=agent_name)

        if namespace:
            # This would need a proper JSON query in production
            # For now, we'll filter in Python
            return [run for run in qs if run.is_in_namespace(namespace)]

        return qs

    def find_singleton_agent(self, agent_name, namespaces):
        """Find existing singleton agent with overlapping namespaces"""
        active_agents = self.active_agents(agent_name=agent_name)

        for agent in active_agents:
            # Check for namespace overlap
            if any(ns in agent.namespaces for ns in namespaces):
                return agent
        return None

    def agents_for_event(self, agent_name, event):
        """Find agents that should receive this event"""
        active_agents = self.active_agents(agent_name=agent_name)

        matching_agents = []
        event_namespace = getattr(event, "namespace", "*")

        for agent in active_agents:
            # Check if agent's namespaces match this event
            if (
                "*" in agent.namespaces
                or event_namespace == "*"
                or event_namespace in agent.namespaces
            ):
                matching_agents.append(agent)

        return matching_agents


AgentRun.add_to_class("objects", AgentRunManager())
