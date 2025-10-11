"""
Agent tools for working with managed files.
These functions can be used by AI agents to interact with file content.
"""
from typing import Optional
from .models import ManagedFile


def get_file_content(
    file_id: int,
    start: Optional[int] = None,
    stop: Optional[int] = None
) -> str:
    """
    Get the OCR-extracted content from a managed file.
    Supports partial reading using start and stop character offsets.

    Args:
        file_id: The ID of the ManagedFile to read
        start: Optional starting character offset (None means from beginning)
        stop: Optional ending character offset (None means to the end)

    Returns:
        The file content as a string, optionally sliced by start/stop offsets

    Raises:
        ValueError: If file not found or OCR not completed
    """
    try:
        managed_file = ManagedFile.objects.get(id=file_id)
    except ManagedFile.DoesNotExist:
        raise ValueError(f"File with id {file_id} not found")

    # Get the most recent successful OCR output
    ocr_output = managed_file.ocr_outputs.filter(success=True).first()

    if not ocr_output:
        raise ValueError(
            f"No OCR output available for file {file_id}. "
            f"Status: {managed_file.ocr_status}"
        )

    content = ocr_output.content

    # Apply slicing if start/stop provided
    if start is not None or stop is not None:
        content = content[start:stop]

    return content
