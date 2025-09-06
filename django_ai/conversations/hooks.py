"""
StateZero hooks with request passing.
"""
from .models import ConversationMessage
from django_ai.automation.workflows.core import engine

def set_message_type(data, request=None):
    """Pre-hook to set message type"""
    data["message_type"] = "user"
    return data


def set_processing_status(data, request=None):
    """Pre-hook to set processing status"""
    if data.get("message_type") == "user":
        data["processing_status"] = "pending"
    return data


def process_user_message(data, request=None):
    """Post-hook to process user messages with request context"""
    if data.get("message_type") == "user":
        try:
            message = (
                ConversationMessage.objects.filter(
                    session_id=data.get("session"),
                    content=data.get("content"),
                    message_type="user",
                )
                .order_by("-timestamp")
                .first()
            )

            if message and engine.executor:
                # Pass request through to async processor
                engine.executor.queue_task(
                    "process_conversation_message", message.id, request
                )
        except Exception:
            pass

    return data


def set_user_context(data, request=None):
    """Pre-hook to set user context on sessions"""
    if request and hasattr(request, "user") and request.user.is_authenticated:
        data["user"] = request.user.id
    return data
