"""
Signal handlers for file management
"""
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db import transaction
from .models import ManagedFile
from .utils import get_parser_path


@receiver(post_save, sender=ManagedFile)
def trigger_ocr_processing(sender, instance, created, **kwargs):
    """
    Trigger OCR processing when a new file is uploaded.
    """
    # Only process newly created files
    if not created:
        return

    # Skip if file already marked as processed
    if instance.ocr_status in ['completed', 'processing']:
        return

    # Get parser class from settings
    parser_path = get_parser_path()

    # Queue the OCR task using the workflow engine executor
    def queue_ocr_task():
        try:
            from django_ai.automation.workflows.core import engine
            if engine.executor:
                engine.executor.queue_task(
                    "process_file_ocr",
                    instance.id,
                    parser_path
                )
                # Mark as pending
                instance.ocr_status = 'pending'
                instance.save(update_fields=['ocr_status'])
        except Exception as e:
            # If queuing fails, mark as failed
            instance.ocr_status = 'failed'
            instance.save(update_fields=['ocr_status'])
            raise

    # Queue after transaction commits
    transaction.on_commit(queue_ocr_task)
