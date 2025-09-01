"""Text extraction with strategy pattern"""

from abc import ABC, abstractmethod
from django.conf import settings
from django.utils.module_loading import import_string
from tika import parser


class TextExtractor(ABC):
    """Abstract base class for text extractors"""

    @abstractmethod
    def extract_text(self, file_path: str, lang: str = "eng") -> str:
        """Extract text from file"""
        pass


class TikaTextExtractor(TextExtractor):
    """Apache Tika text extractor implementation"""

    def extract_text(self, file_path: str, lang: str = "eng") -> str:
        """Extract text using Apache Tika with OCR"""

        headers = {
            "X-Tika-OCRLanguage": lang,
            "X-Tika-PDFOcrStrategy": "ocr_and_text",
        }

        try:
            parsed = parser.from_file(file_path, headers=headers)
            if parsed and parsed.get("content"):
                return parsed["content"].strip()
            return ""
        except Exception as e:
            raise Exception(f"Failed to extract text: {e}") from e


def get_text_extractor() -> TextExtractor:
    """Get the configured text extractor"""
    saas_ai_config = getattr(settings, "SAAS_AI", {})
    extractor_path = saas_ai_config.get(
        "TEXT_EXTRACTOR", "saas_ai.conversations.text_extractor.TikaTextExtractor"
    )

    extractor_class = import_string(extractor_path)

    # Validate it implements the interface
    if not issubclass(extractor_class, TextExtractor):
        raise ValueError(
            f"Text extractor {extractor_path} must inherit from TextExtractor"
        )

    return extractor_class()


# Convenience function
def extract_text(file_path: str, lang: str = "eng") -> str:
    """Extract text using the configured extractor"""
    extractor = get_text_extractor()
    return extractor.extract_text(file_path, lang)
