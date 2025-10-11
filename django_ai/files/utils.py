"""
Utility functions for file processing
"""
from django.conf import settings


def get_parser_path() -> str:
    """
    Get the configured parser class path from settings.

    Returns
    -------
    str
        Full Python path to parser class
    """
    return getattr(
        settings,
        'FILE_PARSER_CLASS',
        'django_ai.files.parsers.TikaParser'
    )
