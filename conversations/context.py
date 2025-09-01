import pusher
from django.conf import settings
import uuid
import time
from contextlib import contextmanager
from threading import local

# Thread-local storage for context
_context_local = local()


class ConversationContext:
    """Conversation context for utilities"""

    def __init__(self, session_id, user=None, agent_instance=None):
        self.session_id = session_id
        self.user = user
        self._agent_instance = agent_instance  # Store agent instance for create_context
        self.channel_name = f"conversation-session-{session_id}"

        # Initialize Pusher client
        pusher_config = getattr(settings, "SAAS_AI_PUSHER", {})
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
            return  # Fail silently if Pusher not configured

        try:
            self.pusher_client.trigger(
                self.channel_name,
                event,
                {**data, "timestamp": time.time(), "id": str(uuid.uuid4())},
            )
        except Exception as e:
            print(f"Pusher error: {e}")  # Fail silently in development


@contextmanager
def chat_context(session_id, user=None, agent_instance=None):
    """Context manager for conversation operations"""
    context = ConversationContext(session_id, user, agent_instance)
    _context_local.context = context
    try:
        yield context
    finally:
        _context_local.context = None


def get_current_context():
    """Get current conversation context"""
    return getattr(_context_local, "context", None)


# Utility functions that work within context
def display_widget(widget_type, data):
    """Display a widget in the current conversation"""
    context = get_current_context()
    if not context:
        raise RuntimeError("display_widget() must be called within chat_context")

    context._send_event("widget", {"widget_type": widget_type, "data": data})


class ResponseStream:
    """Stream responses to the frontend"""

    def __init__(self):
        self.context = get_current_context()
        if not self.context:
            raise RuntimeError("ResponseStream must be created within chat_context")

        self.content = ""
        self._started = False
        self._ended = False

    def start(self, metadata=None):
        """Start the stream"""
        if self._started:
            return

        self._started = True
        self.context._send_event("stream_start", {"metadata": metadata or {}})

    def write(self, chunk):
        """Write a chunk to the stream"""
        if not self._started:
            self.start()

        if self._ended:
            raise RuntimeError("Cannot write to ended stream")

        if chunk:
            self.content += str(chunk)
            self.context._send_event("text_chunk", {"chunk": str(chunk)})

    def end(self):
        """End the stream"""
        if self._ended:
            return

        if not self._started:
            self.start()

        self._ended = True
        self.context._send_event("stream_end", {})

    def __enter__(self):
        """Context manager entry"""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.end()

    def __str__(self):
        """Return the accumulated content"""
        return self.content


def get_file_text(file_id):
    """Utility function to get text from a file within chat context"""
    try:
        from .models import File

        file = File.objects.get(id=file_id)
        return file.extract_text()
    except File.DoesNotExist:
        return ""