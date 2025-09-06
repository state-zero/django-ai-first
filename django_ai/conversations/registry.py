"""
Agent registry for conversation agents.
Users register their agent classes here.
"""

from typing import Dict, Type, Callable
from django.utils.module_loading import import_string
from django.conf import settings


class AgentRegistry:
    """Registry for conversation agent classes"""

    def __init__(self):
        self._agents: Dict[str, Type] = {}

    def register(self, name: str, agent_class: Type):
        """
        Register an agent class with a short name.

        Args:
            name: Short name like 'support', 'sales'
            agent_class: Agent class or import string
        """
        if isinstance(agent_class, str):
            agent_class = import_string(agent_class)

        self._agents[name] = agent_class

    def get(self, name: str) -> Type:
        """
        Get an agent class by name or import path.

        Args:
            name: Short name or full import path

        Returns:
            Agent class

        Raises:
            ValueError: If agent not found
        """
        # Try short name first
        if name in self._agents:
            return self._agents[name]

        # Try as import path
        try:
            return import_string(name)
        except ImportError:
            pass

        raise ValueError(
            f"Agent '{name}' not found. Register with registry.register() or use full import path."
        )

    def list_agents(self) -> Dict[str, Type]:
        """List all registered agents"""
        return self._agents.copy()


# Global registry instance
registry = AgentRegistry()


def register_agent(name: str, agent_class: Type):
    """
    Convenience function to register an agent.

    Example:
    from django_ai.conversations.registry import register_agent

    register_agent('support', 'myapp.agents.SupportAgent')
    register_agent('sales', SalesAgent)
    """
    registry.register(name, agent_class)
