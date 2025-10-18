import pusher
from django.conf import settings
import uuid
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional, Any

from .models import ConversationWidget
from ..utils.json import safe_model_dump

# Thread-safe context storage
_current_session = ContextVar("current_session", default=None)
_current_agent_context = ContextVar("current_agent_context", default=None)
_current_request = ContextVar("current_request", default=None)


class ConversationContext:
    """Conversation context for Pusher utilities"""

    def __init__(self, session_id, request=None):
        self.session_id = session_id
        self.request = request
        self.channel_name = f"private-conversation-session-{session_id}"

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
    """Automatically save context changes using safe JSON serialization"""
    context = _current_agent_context.get()
    session = _current_session.get()
    if context and session:
        session.context = safe_model_dump(context)  # FIX: Use safe serialization
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

    # Create the database record
    widget = ConversationWidget.objects.create(
        session=session,
        widget_type=widget_type,
        widget_data=data
    )

    # Send websocket event for real-time display
    conv_context = ConversationContext(str(session.id), request)
    conv_context._send_event("widget", {
        "widget_type": widget_type,
        "data": data,
        "widget_id": str(widget.id)  # Include ID for tracking
    })

    return widget


def update_widget(widget_id, data=None, display_data=None):
    """Update an existing widget's data and/or display state"""
    session = _current_session.get()
    request = _current_request.get()

    if not session:
        raise RuntimeError(
            "update_widget() must be called within conversation context"
        )

    try:
        widget = ConversationWidget.objects.get(id=widget_id, session=session)
    except ConversationWidget.DoesNotExist:
        raise ValueError(f"Widget {widget_id} not found in current session")

    # Update fields if provided
    updated = False
    if data is not None:
        widget.widget_data = data
        updated = True
    if display_data is not None:
        widget.display_data = display_data
        updated = True

    if updated:
        widget.save()

        # Send websocket event for real-time update
        conv_context = ConversationContext(str(session.id), request)
        conv_context._send_event("widget_update", {
            "widget_id": str(widget.id),
            "widget_type": widget.widget_type,
            "data": widget.widget_data if data is not None else None,
            "display_data": widget.display_data if display_data is not None else None,
        })

    return widget


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


# get_file_text removed - use django_ai.files.tools.get_file_content instead
