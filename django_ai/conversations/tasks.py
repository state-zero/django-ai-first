def process_conversation_message(message_id: int):
    """
    Process a user conversation message and generate agent response.

    Called via the workflow engine when a user message is created.

    Args:
        message_id: ID of the user message to process
    """
    try:
        from .models import ConversationMessage
        from .service import ConversationService
        import logging

        logger = logging.getLogger(__name__)

        message = ConversationMessage.objects.get(id=message_id)

        # Update processing status (if the field exists)
        if hasattr(message, "processing_status"):
            message.processing_status = "processing"
            message.save(update_fields=["processing_status"])

        # Use the conversation service to generate response
        result = ConversationService.send_message(
            session_id=message.session.id,
            message=message.content,
            user=message.session.user,
            files=list(message.files.all()),
        )

        # Update processing status based on result
        if hasattr(message, "processing_status"):
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

        # Try to update status to failed
        try:
            message = ConversationMessage.objects.get(id=message_id)
            if hasattr(message, "processing_status"):
                message.processing_status = "failed"
                message.save(update_fields=["processing_status"])
        except:
            pass