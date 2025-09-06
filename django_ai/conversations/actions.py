from statezero.core.actions import action
from rest_framework import serializers
import uuid
from .models import ConversationSession
from .registry import registry


class StartConversationSerializer(serializers.Serializer):
    agent_path = serializers.CharField(max_length=255)
    context = serializers.JSONField(required=False, default=dict)


@action(name="start_conversation", serializer=StartConversationSerializer)
def start_conversation(agent_path: str, context_kwargs: dict = None, request=None):
    """Create conversation session with agent context in context field"""
    user = request.user if request and request.user.is_authenticated else None
    anonymous_id = f"anon_{uuid.uuid4().hex[:8]}" if not user else ""

    # Create agent and get its context
    agent_class = registry.get(agent_path)
    agent_instance = agent_class()
    agent_context = agent_instance.create_context(
        request=request, **(context_kwargs or {})
    )

    # Create session with agent context in context field
    session = ConversationSession.objects.create(
        agent_path=agent_path,
        user=user,
        anonymous_id=anonymous_id,
        context=agent_context.dict(),  # Agent context goes here
        # metadata stays empty/unused
    )

    return {
        "session_id": str(session.id),
        "status": "created",
        "agent_path": agent_path,
    }
