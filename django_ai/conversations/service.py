"""
Conversation service with clean context separation.
"""

import inspect
import asyncio
from .models import ConversationSession, ConversationMessage
from .registry import registry
from .context import setup_conversation_context, save_context


class ConversationService:
    """Main service for handling conversation operations"""

    @classmethod
    def resolve_agent(cls, agent_path: str):
        """Resolve agent path to agent class"""
        return registry.get(agent_path)

    @classmethod
    def create_session(cls, agent_path, user=None, anonymous_id=None, context=None):
        """Create a new conversation session"""
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
        session = ConversationSession.objects.get(id=session_id)

        # Set up conversation context
        conv_context = setup_conversation_context(session, request)

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
            save_context()

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
            save_context()  # Save context even on error
            ConversationMessage.objects.create(
                session=session, message_type="system", content=f"Error: {str(e)}"
            )
            return {"status": "error", "error": str(e), "session_id": str(session_id)}
