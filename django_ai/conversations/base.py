from abc import ABC, abstractmethod
from typing import Optional, Any
from django.http import HttpRequest
from pydantic import BaseModel

class ConversationAgent(ABC):
    """
    Abstract base class for conversation agents.

    Ensures proper structure and provides convenient access to context.
    """

    # Subclasses must define their Context class
    Context: type[BaseModel] = None

    def __init__(self):
        """Initialize agent with context access"""
        # Validate that Context is defined
        if self.Context is None:
            raise ValueError(f"{self.__class__.__name__} must define a Context class")

        if not issubclass(self.Context, BaseModel):
            raise ValueError(
                f"{self.__class__.__name__}.Context must inherit from BaseModel"
            )

    @property
    def context(self):
        """Convenient access to current agent context"""
        from .context import get_context
        return get_context()
    
    @property
    def session(self):
        """Convenient access to current conversation session"""
        from .context import _current_session
        return _current_session.get()

    @classmethod
    @abstractmethod
    def create_context(
        cls, request: Optional[HttpRequest] = None, **kwargs
    ) -> BaseModel:
        """
        Create the agent's initial context.

        Args:
            request: The HTTP request (for user info, headers, etc.)
            **kwargs: Additional initialization data

        Returns:
            Instance of the agent's Context class
        """
        pass

    @abstractmethod
    async def get_response(
        self, message: str, request: Optional[HttpRequest] = None, **kwargs
    ) -> str:
        """
        Generate a response to the user's message.

        Args:
            message: The user's message
            request: The HTTP request (for current user info, etc.)
            **kwargs: Additional parameters

        Returns:
            The agent's response as a string
        """
        pass
