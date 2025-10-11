"""
Register conversation models with StateZero for testing.
"""

from django.contrib.auth import get_user_model
from statezero.adaptors.django.config import registry
from statezero.adaptors.django.permissions import AllowAllPermission
from statezero.core.config import ModelConfig

from django_ai.conversations.models import (
    ConversationSession,
    ConversationMessage
)
from django_ai.conversations.hooks import (
    set_message_type,
    set_processing_status,
    set_user_context,
)

User = get_user_model()

# Register User model (required for foreign key relations)
registry.register(
    User,
    ModelConfig(
        model=User,
        fields=("username"),
        permissions=[AllowAllPermission]
    ),
)

# Register ConversationSession with StateZero
registry.register(
    ConversationSession,
    ModelConfig(
        model=ConversationSession,
        pre_hooks=[set_user_context],
        permissions=[AllowAllPermission]
    ),
)

# Register ConversationMessage with StateZero
registry.register(
    ConversationMessage,
    ModelConfig(
        model=ConversationMessage,
        pre_hooks=[set_message_type, set_processing_status],
        post_hooks=[],
        permissions=[AllowAllPermission]
    ),
)
