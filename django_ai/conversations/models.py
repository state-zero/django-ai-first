from django.db import models
from django.contrib.auth import get_user_model
import uuid
import mimetypes
import os
import tempfile
import requests
import base64
from django.utils import timezone

from .text_extractor import TikaTextExtractor
from .tasks import process_conversation_message

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
    files = models.ManyToManyField("File", blank=True, related_name="messages")

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
        # _state.adding is True only for new records, even with default pk
        is_new = self._state.adding
        
        super().save(*args, **kwargs)
        
        # After save, if this is a new user message, queue processing
        if is_new and self.message_type == "user":
            from django_ai.automation.workflows.core import engine
            if engine.executor:
                engine.executor.queue_task(
                    "process_conversation_message", self.id, None
                )

class File(models.Model):
    """File attachments for conversations"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession, on_delete=models.CASCADE, related_name="files"
    )

    # Core file fields
    file = models.FileField(upload_to="conversation_files/%Y/%m/%d/", max_length=500)
    filename = models.TextField(blank=True, help_text="Original filename")
    content_type = models.CharField(max_length=255, blank=True, help_text="MIME type")
    file_size = models.PositiveIntegerField(default=0)

    # Text extraction and caching
    extracted_text = models.TextField(
        null=True, blank=True, help_text="Cached extracted text"
    )
    text_extracted_at = models.DateTimeField(
        null=True, blank=True, help_text="When text was last extracted"
    )

    # Additional metadata
    metadata = models.JSONField(
        null=True, blank=True, help_text="Additional file metadata"
    )

    # Timestamps
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def save(self, *args, **kwargs):
        """Set content type, filename, and file size on save"""
        if self.file:
            # Set filename
            self.filename = self.filename or self.file.name

            # Set content type using mimetypes
            if not self.content_type:
                content_type, _ = mimetypes.guess_type(self.filename)
                self.content_type = content_type or "application/octet-stream"

            # Set file size
            self.file_size = self.file.size

            # Reset text cache if file changed
            if self.pk:
                old_instance = File.objects.get(pk=self.pk)
                if old_instance.file != self.file:
                    self.extracted_text = None
                    self.text_extracted_at = None

        super().save(*args, **kwargs)

    def get_base64_encoded_content(self) -> str:
        """Returns the base64-encoded content of the file"""
        if not self.file:
            return None

        with self.file.open("rb") as f:
            file_content = f.read()
        return base64.b64encode(file_content).decode()

    def get_url(self) -> str:
        """Returns the URL of the file"""
        return self.file.url if self.file else ""

    def extract_text(self, force_refresh: bool = False, lang: str = "eng") -> str:
        """
        Extract text from file with caching

        Args:
            force_refresh: Force re-extraction even if cached
            lang: Language for OCR
        """
        # Return cached text if available and not forcing refresh
        if not force_refresh and self.extracted_text and self.text_extracted_at:
            return self.extracted_text

        if not self.file:
            return ""

        try:
            # Create temp file for processing
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                # Download file content
                response = requests.get(self.file.url)
                if response.status_code == 200:
                    temp_file.write(response.content)
                    temp_file.flush()

                    # Extract text
                    extractor = TikaTextExtractor()
                    extracted_text = extractor.extract_text(temp_file.name, lang)

                    # Cache the result
                    self.extracted_text = extracted_text
                    self.text_extracted_at = timezone.now()
                    self.save(update_fields=["extracted_text", "text_extracted_at"])

                    return extracted_text
                else:
                    return ""

        except Exception as e:
            print(f"Error extracting text from {self.filename}: {e}")
            return ""
        finally:
            # Clean up temp file
            if "temp_file" in locals() and os.path.exists(temp_file.name):
                os.remove(temp_file.name)

    def get_file_info(self) -> dict:
        """Get comprehensive file information"""
        return {
            "id": str(self.id),
            "filename": self.filename,
            "content_type": self.content_type,
            "file_size": self.file_size,
            "url": self.get_url(),
            "uploaded_at": self.uploaded_at.isoformat(),
            "has_extracted_text": bool(self.extracted_text),
            "text_length": len(self.extracted_text) if self.extracted_text else 0,
            "text_extracted_at": (
                self.text_extracted_at.isoformat() if self.text_extracted_at else None
            ),
            "metadata": self.metadata or {},
        }

    def __str__(self):
        return f"{self.filename} ({self.content_type})"
