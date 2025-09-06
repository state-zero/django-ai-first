# File: django_ai/django_ai/conversations/base.py
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


# File: django_ai/django_ai/conversations/registry.py
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


# File: django_ai/django_ai/conversations/context.py
"""
Clean context system for conversations.
"""

import pusher
from django.conf import settings
import uuid
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional, Any

# Thread-safe context storage
_current_session = ContextVar("current_session", default=None)
_current_agent_context = ContextVar("current_agent_context", default=None)
_current_request = ContextVar("current_request", default=None)


class ConversationContext:
    """Conversation context for Pusher utilities"""

    def __init__(self, session_id, request=None):
        self.session_id = session_id
        self.request = request
        self.channel_name = f"conversation-session-{session_id}"

        # Initialize Pusher client
        pusher_config = getattr(settings, "DJANGO_AI_PUSHER", {})
        self.pusher_client = (
            pusher.Pusher(
                app_id=pusher_config.get("APP_ID"),
                key=pusher_config.get("KEY"),
                secret=pusher_config.get("SECRET"),
                cluster=pusher_config.get("CLUSTER", "us2"),
                ssl=True,
            )
            if pusher_config.get("KEY")
            else None
        )

    def _send_event(self, event, data):
        """Send event via Pusher"""
        if not self.pusher_client:
            return

        try:
            self.pusher_client.trigger(
                self.channel_name,
                event,
                {**data, "timestamp": time.time(), "id": str(uuid.uuid4())},
            )
        except Exception as e:
            print(f"Pusher error: {e}")


def get_context():
    """Get current agent context - auto-setup if needed"""
    context = _current_agent_context.get()
    if context is None:
        # Auto-setup from current session
        session = _current_session.get()
        if session:
            from .registry import registry

            agent_class = registry.get(session.agent_path)
            context = agent_class.Context(**session.context)
            _current_agent_context.set(context)
    return context


def _auto_save_context():
    """Automatically save context changes"""
    context = _current_agent_context.get()
    session = _current_session.get()
    if context and session:
        session.context = context.dict()
        session.save()


# Utility functions
def display_widget(widget_type, data):
    """Display a widget in the current conversation"""
    session = _current_session.get()
    request = _current_request.get()

    if not session:
        raise RuntimeError(
            "display_widget() must be called within conversation context"
        )

    conv_context = ConversationContext(str(session.id), request)
    conv_context._send_event("widget", {"widget_type": widget_type, "data": data})


class ResponseStream:
    """Stream responses to the frontend"""

    def __init__(self):
        self.session = _current_session.get()
        self.request = _current_request.get()

        if not self.session:
            raise RuntimeError(
                "ResponseStream must be created within conversation context"
            )

        self.conv_context = ConversationContext(str(self.session.id), self.request)
        self.content = ""
        self._started = False
        self._ended = False

    def start(self, metadata=None):
        """Start the stream"""
        if self._started:
            return
        self._started = True
        self.conv_context._send_event("stream_start", {"metadata": metadata or {}})

    def write(self, chunk):
        """Write a chunk to the stream"""
        if not self._started:
            self.start()
        if self._ended:
            raise RuntimeError("Cannot write to ended stream")
        if chunk:
            self.content += str(chunk)
            self.conv_context._send_event("text_chunk", {"chunk": str(chunk)})

    def end(self):
        """End the stream"""
        if self._ended:
            return
        if not self._started:
            self.start()
        self._ended = True
        self.conv_context._send_event("stream_end", {})

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end()

    def __str__(self):
        return self.content


def get_file_text(file_id):
    """Utility function to get text from a file"""
    try:
        from .models import File

        file = File.objects.get(id=file_id)
        return file.extract_text()
    except:
        return ""


# File: django_ai/django_ai/conversations/service.py
"""
Conversation service with clean context separation.
"""

import inspect
import asyncio


class ConversationService:
    """Main service for handling conversation operations"""

    @classmethod
    def resolve_agent(cls, agent_path: str):
        """Resolve agent path to agent class"""
        from .registry import registry

        return registry.get(agent_path)

    @classmethod
    def create_session(cls, agent_path, user=None, anonymous_id=None, context=None):
        """Create a new conversation session"""
        from .models import ConversationSession

        cls.resolve_agent(agent_path)  # Validate

        session = ConversationSession.objects.create(
            agent_path=agent_path,
            user=user,
            anonymous_id=anonymous_id or "",
            context=context or {},
        )
        return session

    @classmethod
    def send_message(cls, session_id, message, user=None, request=None, files=None):
        """Process message with clean context separation"""
        from .models import ConversationSession, ConversationMessage
        from .context import _current_session, _current_request, _auto_save_context

        session = ConversationSession.objects.get(id=session_id)

        # Set context variables so get_context() can auto-load from session
        _current_session.set(session)
        _current_request.set(request)

        try:
            # Create agent instance
            agent_class = cls.resolve_agent(session.agent_path)
            agent_instance = agent_class()
            agent_instance.files = files or []

            # Call agent with clean signature
            if hasattr(agent_instance, "get_response"):
                if inspect.iscoroutinefunction(agent_instance.get_response):
                    response = asyncio.run(
                        agent_instance.get_response(message, request=request)
                    )
                else:
                    response = agent_instance.get_response(message, request=request)
            else:
                # Function-based agent
                if inspect.iscoroutinefunction(agent_instance):
                    response = asyncio.run(agent_instance(message, request=request))
                else:
                    response = agent_instance(message, request=request)

            # Save context changes
            _auto_save_context()

            # Store agent response
            if response:
                ConversationMessage.objects.create(
                    session=session, message_type="agent", content=str(response)
                )

            session.save()
            return {
                "status": "success",
                "response": response,
                "session_id": str(session_id),
            }

        except Exception as e:
            _auto_save_context()  # Save context even on error
            ConversationMessage.objects.create(
                session=session, message_type="system", content=f"Error: {str(e)}"
            )
            return {"status": "error", "error": str(e), "session_id": str(session_id)}


# File: django_ai/django_ai/conversations/actions.py
from statezero.core.actions import action
from rest_framework import serializers
import uuid


class StartConversationSerializer(serializers.Serializer):
    agent_path = serializers.CharField(max_length=255)
    context = serializers.JSONField(required=False, default=dict)


@action(name="start_conversation", serializer=StartConversationSerializer)
def start_conversation(agent_path: str, context_kwargs: dict = None, request=None):
    """Create conversation session with agent context in context field"""
    from .models import ConversationSession
    from .registry import registry

    user = request.user if request and request.user.is_authenticated else None
    anonymous_id = f"anon_{uuid.uuid4().hex[:8]}" if not user else ""

    # Create agent and get its context
    agent_class = registry.get(agent_path)
    agent_instance = agent_class()
    agent_context = agent_class.create_context(
        request=request, **(context_kwargs or {})
    )

    # Create session with agent context in context field
    session = ConversationSession.objects.create(
        agent_path=agent_path,
        user=user,
        anonymous_id=anonymous_id,
        context=agent_context.dict(),  # Agent context goes here
        # metadata stays empty/unused
    )

    return {
        "session_id": str(session.id),
        "status": "created",
        "agent_path": agent_path,
    }


# File: django_ai/django_ai/conversations/tasks.py
"""
Async message processing with request context.
"""
import logging


def process_conversation_message(message_id: int, request=None):
    """Process user message with request context"""
    try:
        from .models import ConversationMessage
        from .service import ConversationService

        logger = logging.getLogger(__name__)
        message = ConversationMessage.objects.get(id=message_id)

        # Update status
        message.processing_status = "processing"
        message.save(update_fields=["processing_status"])

        # Process with request context
        result = ConversationService.send_message(
            session_id=message.session.id,
            message=message.content,
            user=message.session.user,
            request=request,  # Pass through request
            files=list(message.files.all()),
        )

        # Update status
        if result.get("status") == "success":
            message.processing_status = "completed"
        else:
            message.processing_status = "failed"
            logger.error(
                f"Failed to process message {message_id}: {result.get('error')}"
            )

        message.save(update_fields=["processing_status"])

    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error processing conversation message {message_id}: {e}")

        try:
            from .models import ConversationMessage

            message = ConversationMessage.objects.get(id=message_id)
            message.processing_status = "failed"
            message.save(update_fields=["processing_status"])
        except:
            pass
