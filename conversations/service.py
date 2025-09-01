import inspect
from django.urls import resolve
from django.utils.module_loading import import_string
from django.conf import settings
from .context import chat_context
from .models import ConversationSession, ConversationMessage


class ConversationService:
    """Main service for handling conversation operations"""

    @classmethod
    def get_chat_resolver(cls):
        """Get the URLconf for chat agents"""
        chat_urls_path = getattr(settings, "SAAS_AI_CHAT_URLS", None)
        if not chat_urls_path:
            raise ValueError("SAAS_AI_CHAT_URLS setting is required")

        chat_urls_module = import_string(chat_urls_path)
        return getattr(chat_urls_module, "chat_urlpatterns", [])

    @classmethod
    def resolve_agent(cls, agent_path):
        """Use Django's URL resolution for agents"""
        from django.urls.resolvers import URLResolver

        try:
            resolver = URLResolver(r"^", cls.get_chat_resolver())
            return resolver.resolve(agent_path)
        except Exception as e:
            raise ValueError(f"No agent found for path '{agent_path}': {e}")

    @classmethod
    def create_session(cls, agent_path, user=None, anonymous_id=None, context=None):
        """Create a new conversation session"""
        # Validate agent path exists
        cls.resolve_agent(agent_path)

        session = ConversationSession.objects.create(
            agent_path=agent_path,
            user=user,
            anonymous_id=anonymous_id or "",
            context=context or {},
        )

        return session

    @classmethod
    def send_message(cls, session_id, message, user=None):
        """Process message with agent"""
        # Get session
        session = ConversationSession.objects.get(id=session_id)

        # Store user message
        ConversationMessage.objects.create(
            session=session, message_type="user", content=message
        )

        # Resolve agent
        resolved = cls.resolve_agent(session.agent_path)
        agent_handler = resolved.func

        # Create agent instance if class-based
        agent_instance = None
        if hasattr(agent_handler, "get_response"):
            agent_instance = agent_handler()

        # Run with chat context
        with chat_context(session_id, user, agent_instance) as ctx:
            # Build session context (data for agent)
            session_context = {
                "user_id": user.id if user else None,
                "session_id": str(session_id),
                "anonymous_id": session.anonymous_id,
                **session.context,
            }

            try:
                if agent_instance:
                    # Class-based agent
                    if inspect.iscoroutinefunction(agent_instance.get_response):
                        import asyncio

                        response = asyncio.run(
                            agent_instance.get_response(
                                message, session_context, **resolved.kwargs
                            )
                        )
                    else:
                        response = agent_instance.get_response(
                            message, session_context, **resolved.kwargs
                        )
                else:
                    # Function-based agent
                    if inspect.iscoroutinefunction(agent_handler):
                        import asyncio

                        response = asyncio.run(
                            agent_handler(message, session_context, **resolved.kwargs)
                        )
                    else:
                        response = agent_handler(
                            message, session_context, **resolved.kwargs
                        )

                # Store agent response
                if response:
                    ConversationMessage.objects.create(
                        session=session, message_type="agent", content=str(response)
                    )

                # Update session timestamp
                session.save()

                return {
                    "status": "success",
                    "response": response,
                    "session_id": str(session_id),
                }

            except Exception as e:
                # Store error message
                ConversationMessage.objects.create(
                    session=session, message_type="system", content=f"Error: {str(e)}"
                )

                return {
                    "status": "error",
                    "error": str(e),
                    "session_id": str(session_id),
                }
