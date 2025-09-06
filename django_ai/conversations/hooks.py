"""
Hook functions for conversation models that users can register with StateZero.

Example usage:
from statezero.core.config import ModelConfig
from statezero import register_model
from django_ai.conversations.hooks import process_user_message, set_message_type, set_processing_status

register_model(
    ConversationMessage,
    ModelConfig(
        model=ConversationMessage,
        pre_hooks=[set_message_type, set_processing_status],
        post_hooks=[process_user_message],
        permissions=[IsAuthenticated, HasConversationAccess]
    )
)
"""


def set_message_type(data, request=None):
    """
    Pre-hook to set message type based on request context.

    Prevents API users from setting message_type directly - only users (authenticated or anonymous)
    can create "user" messages. Agent and system messages are created server-side only.

    Args:
        data: The incoming message data
        request: Django request object

    Returns:
        dict: Modified data with message_type set correctly
    """
    # All API requests create user messages - authenticated or anonymous
    # The session itself handles the user/anonymous distinction
    data["message_type"] = "user"

    return data


def set_processing_status(data, request=None):
    """
    Pre-hook to set processing status for user messages.

    Sets processing_status to "pending" for new user messages so UI can show loading state.

    Args:
        data: The incoming message data
        request: Django request object

    Returns:
        dict: Modified data with processing_status set
    """
    # Only set pending status for user messages (set by set_message_type above)
    if data.get("message_type") == "user":
        data["processing_status"] = "pending"

    return data


def process_user_message(data, request=None):
    """
    Post-hook to process user messages and generate agent responses.

    Triggers when a new ConversationMessage with message_type="user" is created.
    Uses the workflow engine to queue async processing.

    Args:
        data: The validated message data
        request: Django request object

    Returns:
        dict: Unmodified data (this is a side-effect hook)
    """
    # Only process new user messages
    if data.get("message_type") == "user":
        try:
            from .models import ConversationMessage
            from django_ai.automation.workflows.core import engine

            # Find the message that was just created
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
                # Use the workflow engine to queue processing
                engine.executor.queue_task("process_conversation_message", message.id)
        except Exception:
            # Fail silently - don't break the message creation
            pass

    return data


def set_user_context(data, request=None):
    """
    Pre-hook to set user context on conversation sessions.

    Automatically sets user field from authenticated request user.

    Args:
        data: The incoming session data
        request: Django request object

    Returns:
        dict: Modified data with user set
    """
    if request and hasattr(request, "user") and request.user.is_authenticated:
        data["user"] = request.user.id

    return data
