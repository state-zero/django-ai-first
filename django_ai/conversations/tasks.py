"""
Async message processing with request context.
"""
from .models import ConversationMessage
from .service import ConversationService
import logging

def process_conversation_message(message_id: int, request=None):
    """Process user message with request context"""
    try:
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
        import logging

        logger = logging.getLogger(__name__)
        logger.error(f"Error processing conversation message {message_id}: {e}")

        try:
            message = ConversationMessage.objects.get(id=message_id)
            message.processing_status = "failed"
            message.save(update_fields=["processing_status"])
        except:
            pass
