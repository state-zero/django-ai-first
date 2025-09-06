import inspect
from django.conf import settings
from .context import chat_context
from .models import ConversationSession, ConversationMessage
from .registry import registry

class ConversationService:
    """Main service for handling conversation operations"""

    @classmethod
    def resolve_agent(cls, agent_path: str):
        """
        Resolve agent path to agent class.

        Args:
            agent_path: Short name like 'support' or full path like 'myapp.agents.SupportAgent'

        Returns:
            Agent class
        """
        return registry.get(agent_path)

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
    def send_message(cls, session_id, message, user=None, files=None):
        """Process message with agent"""
        # Get session
        session = ConversationSession.objects.get(id=session_id)

        # Store user message
        user_message = ConversationMessage.objects.create(
            session=session, message_type="user", content=message
        )

        # Attach files to the message
        if files:
            user_message.files.set(files)

        # Resolve agent
        agent_class = cls.resolve_agent(session.agent_path)
        agent_instance = agent_class()

        # Attach files to agent instance for access in get_response
        agent_instance.files = files or []

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
                # Call agent
                if hasattr(agent_instance, "get_response"):
                    if inspect.iscoroutinefunction(agent_instance.get_response):
                        import asyncio

                        response = asyncio.run(
                            agent_instance.get_response(message, session_context)
                        )
                    else:
                        response = agent_instance.get_response(message, session_context)
                else:
                    # Function-based agent
                    if inspect.iscoroutinefunction(agent_instance):
                        import asyncio

                        response = asyncio.run(agent_instance(message, session_context))
                    else:
                        response = agent_instance(message, session_context)

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


# Example user code:

"""
# myapp/agents.py
from pydantic import BaseModel

class SupportAgent:
    class Context(BaseModel):
        user_id: int = 0
        tier: str = "basic"
    
    def create_context(self):
        return self.Context(
            user_id=self.user.id if hasattr(self, 'user') and self.user else 0
        )
    
    def get_response(self, message, session_context):
        if "hello" in message.lower():
            return "Hello! How can I help you today?"
        return "I'm here to help!"

class SalesAgent:
    def get_response(self, message, session_context):
        return "Thanks for your interest! Let me connect you with sales."


# myapp/apps.py or myapp/__init__.py
from django_ai.conversations.registry import register_agent
from .agents import SupportAgent, SalesAgent

register_agent('support', SupportAgent)
register_agent('sales', SalesAgent)

# Now users can create sessions with:
# ConversationSession(agent_path="support", ...)
# ConversationSession(agent_path="sales", ...)
#
"""
