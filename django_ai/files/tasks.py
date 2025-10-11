"""
Background tasks for file processing
"""
from django.db import transaction
from django.utils import timezone
import time

from .exceptions import OCRException
from .utils import get_parser_path


def process_file_ocr(file_id: int, parser_path: str = None) -> dict:
    """
    Process OCR for a managed file.

    Parameters
    ----------
    file_id : int
        ID of the ManagedFile to process
    parser_path : str, optional
        Full path to parser class. If None, uses settings.FILE_PARSER_CLASS

    Returns
    -------
    dict
        Processing result with status and details
    """
    from .models import ManagedFile, OCROutput
    from .parsers import get_parser_class

    start_time = time.time()

    try:
        # Get the file
        managed_file = ManagedFile.objects.get(id=file_id)

        # Update status to processing
        managed_file.ocr_status = 'processing'
        managed_file.save(update_fields=['ocr_status'])

        # Get parser
        if parser_path is None:
            parser_path = get_parser_path()

        parser = get_parser_class(parser_path)
        parser_version = parser.get_version()  # Cache version

        # Parse the file
        try:
            content, metadata = parser.parse(managed_file.file.path)
            processing_time_ms = int((time.time() - start_time) * 1000)

            # Save OCR output
            with transaction.atomic():
                OCROutput.objects.create(
                    managed_file=managed_file,
                    content=content,
                    parser_class=parser_path,
                    parser_version=parser_version,
                    metadata=metadata,
                    processing_time_ms=processing_time_ms,
                    success=True
                )

                # Update file status
                managed_file.ocr_status = 'completed'
                managed_file.save(update_fields=['ocr_status'])

            return {
                "status": "success",
                "file_id": file_id,
                "processing_time_ms": processing_time_ms,
                "content_length": len(content),
                "timestamp": timezone.now().isoformat()
            }

        except OCRException as e:
            # OCR/parsing failed - log the error
            error_message = str(e)

            with transaction.atomic():
                OCROutput.objects.create(
                    managed_file=managed_file,
                    content="",
                    parser_class=parser_path,
                    parser_version=parser_version,
                    metadata={},
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    success=False,
                    error_message=error_message
                )

                managed_file.ocr_status = 'failed'
                managed_file.save(update_fields=['ocr_status'])

            return {
                "status": "failed",
                "file_id": file_id,
                "error": error_message,
                "timestamp": timezone.now().isoformat()
            }

    except ManagedFile.DoesNotExist:
        return {
            "status": "failed",
            "file_id": file_id,
            "error": f"ManagedFile with id {file_id} not found",
            "timestamp": timezone.now().isoformat()
        }

    except Exception as e:
        # Unexpected error - log it properly
        error_message = f"Unexpected error: {str(e)}"

        # Try to mark as failed, but if managed_file doesn't exist, that's okay
        if 'managed_file' in locals():
            try:
                managed_file.ocr_status = 'failed'
                managed_file.save(update_fields=['ocr_status'])
            except Exception:
                # File might have been deleted, just log
                pass

        return {
            "status": "failed",
            "file_id": file_id,
            "error": error_message,
            "timestamp": timezone.now().isoformat()
        }
