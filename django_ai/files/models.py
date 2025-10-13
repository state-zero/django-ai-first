from django.db import models
from django.contrib.auth import get_user_model


class Folder(models.Model):
    """Virtual folder for organizing files"""
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='subfolders'
    )
    owner = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name='folders'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        models.UniqueConstraint(
            fields=['name', 'parent', 'owner'],
            name='unique_folder_name_per_parent'
        )
        indexes = [
            models.Index(fields=['owner', 'parent']),
            models.Index(fields=['name']),
        ]

    def __str__(self):
        return self.name


class ManagedFile(models.Model):
    """Reusable file that can be referenced across the app"""
    file = models.FileField(upload_to='files/%Y/%m/%d/')
    filename = models.CharField(max_length=255)

    folder = models.ForeignKey(
        Folder,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='files'
    )

    owner = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name='managed_files'
    )

    # File metadata
    file_size = models.BigIntegerField(help_text="File size in bytes")
    mime_type = models.CharField(max_length=255, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # OCR status tracking
    ocr_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
        ],
        default='pending'
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['owner', '-created_at']),
            models.Index(fields=['folder', '-created_at']),
            models.Index(fields=['ocr_status']),
            models.Index(fields=['mime_type']),
        ]

    def __str__(self):
        return self.filename

    def save(self, *args, **kwargs):
        # Auto-populate fields from file if not provided
        if self.file:
            if not self.filename:
                self.filename = self.file.name
            if not self.file_size:
                self.file_size = self.file.size
        super().save(*args, **kwargs)


class OCROutput(models.Model):
    """Stores OCR output for a file"""
    managed_file = models.ForeignKey(
        ManagedFile,
        on_delete=models.CASCADE,
        related_name='ocr_outputs'
    )

    # OCR content
    content = models.TextField(blank=True)

    # Parser information
    parser_class = models.CharField(
        max_length=255,
        help_text="Full path to the parser class used (e.g., 'django_ai.files.parsers.TikaParser')"
    )
    parser_version = models.CharField(max_length=50, blank=True)

    # Metadata from parser
    metadata = models.JSONField(default=dict, blank=True)

    # Processing details
    processed_at = models.DateTimeField(auto_now_add=True)
    processing_time_ms = models.IntegerField(
        null=True,
        blank=True,
        help_text="Time taken to process in milliseconds"
    )

    # Error tracking
    error_message = models.TextField(blank=True)
    success = models.BooleanField(default=True)

    class Meta:
        ordering = ['-processed_at']
        indexes = [
            models.Index(fields=['managed_file', '-processed_at']),
            models.Index(fields=['parser_class']),
            models.Index(fields=['success']),
        ]

    def __str__(self):
        return f"OCR for {self.managed_file.filename} ({self.parser_class})"
