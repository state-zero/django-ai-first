from abc import ABC, abstractmethod
from typing import Optional, Any, List, Callable
from django.http import HttpRequest
from pydantic import BaseModel


class ConversationAgent(ABC):
    """
    Abstract base class for conversation agents.

    Ensures proper structure and provides convenient access to context.
    Supports dynamic tool loadouts via Tools configuration.
    """

    # Subclasses must define their Context class
    Context: type[BaseModel] = None

    # Subclasses can optionally define Tools configuration
    class Tools:
        base: List[str] = []  # Base tools (always available)
        available: List[str] = []  # Tools that can be added as additional tools
        max_additional: Optional[int] = 5  # Max additional tools per session

    def __init__(self):
        """Initialize agent with context access and validate tools"""
        # Validate that Context is defined
        if self.Context is None:
            raise ValueError(f"{self.__class__.__name__} must define a Context class")

        if not issubclass(self.Context, BaseModel):
            raise ValueError(
                f"{self.__class__.__name__}.Context must inherit from BaseModel"
            )

        # Validate Tools configuration
        self._validate_tools_config()

        # Validate base tools exist in registry
        self._validate_base_tools()

    def _validate_tools_config(self):
        """Validate Tools class has correct attributes (prevent typos)"""
        allowed_attrs = {'base', 'available', 'max_additional'}

        # Get all non-dunder attributes from Tools class
        tools_attrs = {
            attr for attr in dir(self.Tools)
            if not attr.startswith('_')
        }

        # Check for invalid attributes
        invalid_attrs = tools_attrs - allowed_attrs
        if invalid_attrs:
            raise ValueError(
                f"{self.__class__.__name__}.Tools has invalid attributes: {', '.join(invalid_attrs)}. "
                f"Allowed attributes are: {', '.join(allowed_attrs)}"
            )

        # Validate available doesn't contain base tools
        base_set = set(self.Tools.base)
        available_set = set(self.Tools.available)
        overlap = base_set & available_set

        if overlap:
            raise ValueError(
                f"{self.__class__.__name__}.Tools.available contains base tools: {', '.join(overlap)}. "
                f"Available tools should not include base tools (they are always included)."
            )

    def _validate_base_tools(self):
        """Validate that all base tools exist in registry"""
        from django_ai.tools import normalize_tool_reference

        try:
            # Normalize all tool references (supports strings and functions)
            normalized = []
            for tool_ref in self.Tools.base:
                normalized.append(normalize_tool_reference(tool_ref))
            # Store normalized names back
            self.Tools.base = normalized
        except ValueError as e:
            raise ValueError(
                f"{self.__class__.__name__}.Tools.base validation failed: {e}"
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

    @property
    def tools(self) -> List[Callable]:
        """
        Get current tools: base tools + additional_tools from session.

        Returns:
            List of callable tool functions with preserved signatures
        """
        from django_ai.tools import get_tool, normalize_tool_reference

        # Get base tools (already normalized in __init__)
        tool_functions = [get_tool(name) for name in self.Tools.base]

        # Add session-specific additional tools
        if self.session:
            try:
                session_loadout = self.session.tool_loadout
                # Normalize additional tools (supports strings and functions)
                for tool_ref in session_loadout.loadout.additional_tools:
                    tool_name = normalize_tool_reference(tool_ref)
                    tool_functions.append(get_tool(tool_name))
            except Exception:
                # No loadout exists, just use base tools
                pass

        return tool_functions

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
        self, message: str, request: Optional[HttpRequest] = None, files=None, **kwargs
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
