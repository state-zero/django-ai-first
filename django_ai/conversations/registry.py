"""
Agent registry for conversation agents.
Only accepts ConversationAgent subclasses for type safety.
"""

from typing import Dict, Type
from django.utils.module_loading import import_string
from django.conf import settings

class AgentRegistry:
    """Registry for conversation agent classes - only accepts ConversationAgent subclasses"""

    def __init__(self):
        self._agents: Dict[str, Type] = {}

    def register(self, name: str, agent_class: Type):
        """
        Register an agent class with a short name.

        Args:
            name: Short name like 'support', 'sales'
            agent_class: ConversationAgent subclass or import string

        Raises:
            ValueError: If agent_class is not a ConversationAgent subclass
        """
        if isinstance(agent_class, str):
            agent_class = import_string(agent_class)

        # Validate that it's a ConversationAgent subclass
        from .base import ConversationAgent

        if not (
            isinstance(agent_class, type) and issubclass(agent_class, ConversationAgent)
        ):
            raise ValueError(
                f"Agent '{name}' must be a subclass of ConversationAgent. "
                f"Got {agent_class.__name__ if hasattr(agent_class, '__name__') else type(agent_class)}"
            )

        # Additional validation - ensure it's not the abstract base class itself
        if agent_class is ConversationAgent:
            raise ValueError(
                f"Cannot register the abstract ConversationAgent class itself. "
                f"Register a concrete subclass instead."
            )

        self._agents[name] = agent_class

    def get(self, name: str) -> Type:
        """
        Get an agent class by name or import path.

        Args:
            name: Short name or full import path

        Returns:
            ConversationAgent subclass

        Raises:
            ValueError: If agent not found or not a valid ConversationAgent
        """
        # Try short name first
        if name in self._agents:
            return self._agents[name]

        # Try as import path
        try:
            agent_class = import_string(name)

            # Validate it's a ConversationAgent subclass
            from .base import ConversationAgent

            if not (
                isinstance(agent_class, type)
                and issubclass(agent_class, ConversationAgent)
            ):
                raise ValueError(
                    f"Agent '{name}' must be a subclass of ConversationAgent. "
                    f"Got {agent_class.__name__ if hasattr(agent_class, '__name__') else type(agent_class)}"
                )

            return agent_class

        except ImportError:
            pass

        raise ValueError(
            f"Agent '{name}' not found. Register with registry.register() or use full import path to a ConversationAgent subclass."
        )

    def list_agents(self) -> Dict[str, Type]:
        """List all registered agents"""
        return self._agents.copy()


# Global registry instance
registry = AgentRegistry()


def register_agent(name: str, agent_class: Type):
    """
    Convenience function to register a ConversationAgent subclass.

    Args:
        name: Short name for the agent
        agent_class: ConversationAgent subclass or import string

    Example:
        from django_ai.conversations.registry import register_agent
        from myapp.agents import SupportAgent

        register_agent('support', SupportAgent)
        register_agent('sales', 'myapp.agents.SalesAgent')

    Raises:
        ValueError: If agent_class is not a ConversationAgent subclass
    """
    registry.register(name, agent_class)