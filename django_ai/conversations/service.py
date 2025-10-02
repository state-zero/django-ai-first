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
        """Process message with clean context separation"""
        from .models import ConversationSession, ConversationMessage
        from .context import _current_session, _current_request, _auto_save_context

        session = ConversationSession.objects.get(id=session_id)

        # Set context variables so get_context() can auto-load from session
        _current_session.set(session)
        _current_request.set(request)

        try:
            # Create agent instance
            agent_class = cls.resolve_agent(session.agent_path)
            agent_instance = agent_class()

            # Call agent with clean signature
            if hasattr(agent_instance, "get_response"):
                if inspect.iscoroutinefunction(agent_instance.get_response):
                    response = asyncio.run(
                        agent_instance.get_response(message, request=request, files=files)
                    )
                else:
                    response = agent_instance.get_response(message, request=request, files=files)
            else:
                # Function-based agent
                if inspect.iscoroutinefunction(agent_instance):
                    response = asyncio.run(agent_instance(message, request=request, files=files))
                else:
                    response = agent_instance(message, request=request, files=files)

            # Save context changes
            _auto_save_context()

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
            _auto_save_context()  # Save context even on error
            ConversationMessage.objects.create(
                session=session, message_type="system", content=f"Error: {str(e)}"
            )
            return {"status": "error", "error": str(e), "session_id": str(session_id)}
