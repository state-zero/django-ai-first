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


def setup_conversation_context(session, request=None):
    """Set up context for conversation processing"""
    from .registry import registry

    # Set basic context
    _current_session.set(session)
    _current_request.set(request)

    # Load agent context from session.context (not metadata)
    agent_class = registry.get(session.agent_path)
    agent_instance = agent_class()

    # session.context contains the agent's context dict
    agent_context = agent_instance.Context(**session.context)
    _current_agent_context.set(agent_context)

    return ConversationContext(str(session.id), request)


def get_context():
    """Get current agent context (the main context mechanism)"""
    return _current_agent_context.get()


def save_context():
    """Save current agent context back to session.context"""
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
    except File.DoesNotExist:
        return ""
