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
        from .context import _current_session, _current_request
        from ..utils.json import safe_model_dump

        session = ConversationSession.objects.get(id=session_id)

        # Set session/request context vars (for widgets, streaming, etc.)
        sess_token = _current_session.set(session)
        req_token = _current_request.set(request)

        try:
            # Create agent instance and hydrate context
            agent_class = cls.resolve_agent(session.agent_path)
            agent_instance = agent_class()
            agent_instance.context = agent_class.Context(**session.context)

            # Pick callable: method get_response or the instance itself
            call = agent_instance.get_response if hasattr(agent_instance, "get_response") else agent_instance

            # Call synchronously
            response = call(message, request=request, files=files)

            # Persist context changes
            session.context = safe_model_dump(agent_instance.context)
            session.save()

            # Store agent response
            if response is not None:
                ConversationMessage.objects.create(
                    session=session, message_type="agent", content=str(response)
                )

            return {
                "status": "success",
                "response": response,
                "session_id": str(session_id),
            }

        except Exception as e:
            # Persist context even on error
            session.context = safe_model_dump(agent_instance.context)
            session.save()
            ConversationMessage.objects.create(
                session=session, message_type="system", content=f"Error: {str(e)}"
            )
            return {"status": "error", "error": str(e), "session_id": str(session_id)}

        finally:
            # Restore original context values
            _current_session.reset(sess_token)
            _current_request.reset(req_token)
