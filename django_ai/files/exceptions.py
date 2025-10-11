"""
Custom exceptions for file processing
"""


class OCRException(Exception):
    """
    Exception raised when OCR/file parsing fails.

    This provides clear error messages to library users about what went wrong
    during file processing.

    Examples:
        - Parser not installed
        - Unsupported file format
        - File corrupted
        - Parsing timeout
    """
    pass
