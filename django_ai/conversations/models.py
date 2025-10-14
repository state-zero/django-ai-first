from django.db import models, transaction
from django.contrib.auth import get_user_model
import uuid

User = get_user_model()


class ConversationSession(models.Model):
    """A conversation session with an AI agent"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent_path = models.CharField(
        max_length=255, 
        help_text="Agent name from registry like 'support', 'sales', etc."
    )
    help_text = "Agent name from registry like 'support', 'sales', etc."

    # Flexible user reference
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.CASCADE)
    anonymous_id = models.CharField(
        max_length=255, blank=True, help_text="For anonymous users"
    )

    # Session data
    context = models.JSONField(default=dict, help_text="Session context data")
    metadata = models.JSONField(default=dict, help_text="UI and custom metadata")

    # Status
    STATUS_CHOICES = [
        ("active", "Active"),
        ("completed", "Completed"),
        ("archived", "Archived"),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Conversation {self.id} - {self.agent_path}"


class ConversationMessage(models.Model):
    """Individual messages in a conversation"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession, on_delete=models.CASCADE, related_name="messages"
    )

    MESSAGE_TYPES = [
        ("user", "User Message"),
        ("agent", "Agent Response"),
        ("system", "System Message"),
        ("widget", "Widget Display"),
    ]

    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPES)
    content = models.TextField()

    # For widgets
    component_type = models.CharField(max_length=100, blank=True)
    component_data = models.JSONField(default=dict)
    files = models.ManyToManyField("files.ManagedFile", blank=True, related_name="conversation_messages")
    tables = models.ManyToManyField("tables.Table", blank=True, related_name="conversation_messages")

    # Processing state for optimistic updates
    PROCESSING_STATUS = [
        ("pending", "Pending"),  # Just created, waiting for agent
        ("processing", "Processing"),  # Agent is working on it
        ("completed", "Completed"),  # Agent response saved
        ("failed", "Failed"),  # Agent processing failed
    ]
    processing_status = models.CharField(
        max_length=20,
        choices=PROCESSING_STATUS,
        default="completed",
        help_text="Processing status for user messages",
    )

    # Metadata
    metadata = models.JSONField(default=dict)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["timestamp"]

    def __str__(self):
        return f"{self.message_type}: {self.content[:50]}"

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        
        super().save(*args, **kwargs)
        
        # Queue the task AFTER the transaction commits
        if is_new and self.message_type == "user":
            def queue_processing():
                from django_ai.automation.workflows.core import engine
                if engine.executor:
                    engine.executor.queue_task(
                        "process_conversation_message", self.id, None
                    )
            
            transaction.on_commit(queue_processing)
            
class ConversationWidget(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="widgets"
    )

    widget_type = models.CharField(max_length=100)
    widget_data = models.JSONField()

    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp']


class SessionToolLoadout(models.Model):
    """
    Simple mapping between ConversationSession and ToolLoadout.

    Links a conversation session to its tool configuration.
    """
    session = models.OneToOneField(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="tool_loadout",
        help_text="The conversation session"
    )

    loadout = models.ForeignKey(
        'tools.ToolLoadout',
        on_delete=models.CASCADE,
        help_text="The tool loadout configuration"
    )

    class Meta:
        verbose_name = "Session Tool Loadout"
        verbose_name_plural = "Session Tool Loadouts"

    def __str__(self):
        return f"Session {self.session.id} -> Loadout {self.loadout.id}"

    def clean(self):
        """Validate the loadout is valid for this session's agent"""
        from django_ai.conversations.registry import registry

        try:
            agent_class = registry.get(self.session.agent_path)
            self.loadout.validate_for_agent(agent_class)
        except Exception as e:
            from django.core.exceptions import ValidationError
            raise ValidationError(str(e))
