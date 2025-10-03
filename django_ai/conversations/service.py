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
        """Process message with clean context separation (sync only)."""
        from .models import ConversationSession, ConversationMessage
        from .context import _current_session, _current_request, _auto_save_context

        session = ConversationSession.objects.get(id=session_id)

        # Set context vars (remember tokens so we can restore them)
        sess_token = _current_session.set(session)
        req_token  = _current_request.set(request)

        try:
            # Create agent instance
            agent_class = cls.resolve_agent(session.agent_path)
            agent_instance = agent_class()

            # Pick callable: method get_response or the instance itself
            call = agent_instance.get_response if hasattr(agent_instance, "get_response") else agent_instance

            # Call synchronously
            response = call(message, request=request, files=files)

            # Save context changes
            _auto_save_context()

            # Store agent response
            if response is not None:
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

        finally:
            # Restore original context values
            _current_session.reset(sess_token)
            _current_request.reset(req_token)
