"""
File parser implementations using strategy pattern.
Parsers extract text content from various file types.
"""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Union, Tuple, Dict, Any, Optional
import io
import time
import os

from .exceptions import OCRException


class BaseParser(ABC):
    """Abstract base class for file parsers"""

    @abstractmethod
    def parse(
        self,
        file_source: Union[str, Path, bytes, io.IOBase]
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Parse a file and extract text content.

        Parameters
        ----------
        file_source : Union[str, Path, bytes, io.IOBase]
            The file to parse. Can be a file path, bytes, or file-like object.

        Returns
        -------
        content : str
            Extracted text content from the file.
        metadata : Dict[str, Any]
            File metadata including parser-specific information.

        Raises
        ------
        OCRException
            If parsing fails for any reason.
        """
        pass

    @abstractmethod
    def get_version(self) -> str:
        """Return the version of the parser implementation"""
        pass


class TikaParser(BaseParser):
    """
    Parser using Apache Tika for document text extraction.
    Supports PDFs (including OCR via Tesseract), Office docs, and more.
    """

    def __init__(self, server_url: Optional[str] = None):
        """
        Initialize Tika parser.

        Parameters
        ----------
        server_url : Optional[str]
            URL of Tika server. If None, uses Django setting
            TIKA_SERVER_ENDPOINT or local Tika.
        """
        self.server_url = server_url

    def parse(
        self,
        file_source: Union[str, Path, bytes, io.IOBase]
    ) -> Tuple[str, Dict[str, Any]]:
        """Parse file using Apache Tika"""
        try:
            from tika import parser
            from django.conf import settings
        except ImportError:
            raise OCRException(
                "tika-python is not installed. Install with: pip install tika"
            )

        start_time = time.time()

        try:
            # Determine server URL from settings
            server_url = self.server_url or getattr(settings, 'TIKA_SERVER_ENDPOINT', None) or os.getenv('TIKA_SERVER_ENDPOINT', None)

            # Parse based on input type
            if isinstance(file_source, (str, Path)):
                if server_url:
                    parsed = parser.from_file(str(file_source), serverEndpoint=server_url)
                else:
                    parsed = parser.from_file(str(file_source))
            else:
                # Handle bytes or file-like object
                if hasattr(file_source, "read"):
                    content_bytes = file_source.read()
                    if hasattr(file_source, "seek"):
                        file_source.seek(0)  # Reset for potential reuse
                else:
                    content_bytes = file_source

                if server_url:
                    parsed = parser.from_buffer(content_bytes, serverEndpoint=server_url)
                else:
                    parsed = parser.from_buffer(content_bytes)

            # Extract content and metadata
            content = parsed.get("content", "") or ""
            metadata = parsed.get("metadata", {})
            metadata_dict = dict(metadata) if metadata else {}

            # Add processing time
            metadata_dict["processing_time_ms"] = int((time.time() - start_time) * 1000)

            return content, metadata_dict

        except Exception as e:
            raise OCRException(f"Tika parsing failed: {str(e)}")

    def get_version(self) -> str:
        """Get Tika version"""
        try:
            import tika
            return getattr(tika, '__version__', 'unknown')
        except ImportError:
            return 'not_installed'

def get_parser_class(parser_path: str) -> BaseParser:
    """
    Dynamically load a parser class from a string path.

    Parameters
    ----------
    parser_path : str
        Full Python path to parser class (e.g., 'django_ai.files.parsers.TikaParser')

    Returns
    -------
    BaseParser
        Instance of the parser class

    Raises
    ------
    ImportError
        If the parser class cannot be imported
    """
    module_path, class_name = parser_path.rsplit('.', 1)
    module = __import__(module_path, fromlist=[class_name])
    parser_class = getattr(module, class_name)
    return parser_class()
