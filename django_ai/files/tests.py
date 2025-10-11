from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.conf import settings
import tempfile
import os

from .models import Folder, ManagedFile, OCROutput
from .parsers import TikaParser, get_parser_class
from .tasks import process_file_ocr
from .tools import get_file_content
from .exceptions import OCRException

User = get_user_model()


class FolderModelTest(TestCase):
    """Test Folder model"""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def test_create_folder(self):
        """Test creating a simple folder"""
        folder = Folder.objects.create(
            name="Documents",
            owner=self.user
        )
        self.assertEqual(folder.name, "Documents")
        self.assertIsNone(folder.parent)

    def test_nested_folders(self):
        """Test nested folder hierarchy"""
        parent = Folder.objects.create(name="Parent", owner=self.user)
        child = Folder.objects.create(name="Child", parent=parent, owner=self.user)
        grandchild = Folder.objects.create(name="Grandchild", parent=child, owner=self.user)

        self.assertEqual(grandchild.parent, child)
        self.assertEqual(child.parent, parent)
        self.assertIsNone(parent.parent)

    def test_unique_together(self):
        """Test that folder names must be unique within parent"""
        parent = Folder.objects.create(name="Parent", owner=self.user)
        Folder.objects.create(name="Child", parent=parent, owner=self.user)

        # Should fail - duplicate name in same parent
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            Folder.objects.create(name="Child", parent=parent, owner=self.user)


class ManagedFileModelTest(TestCase):
    """Test ManagedFile model"""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def test_create_file(self):
        """Test creating a file"""
        file_content = ContentFile(b"Test content", name="test.txt")
        managed_file = ManagedFile.objects.create(
            file=file_content,
            owner=self.user,
            file_size=100,
            mime_type="text/plain"
        )
        self.assertEqual(managed_file.filename, "test.txt")
        self.assertEqual(managed_file.ocr_status, "pending")

    def test_auto_populate_filename(self):
        """Test that filename is auto-populated from file"""
        file_content = ContentFile(b"Test content", name="document.pdf")
        managed_file = ManagedFile.objects.create(
            file=file_content,
            owner=self.user,
            file_size=100
        )
        self.assertEqual(managed_file.filename, "document.pdf")

    def test_auto_populate_file_size(self):
        """Test that file_size is auto-populated"""
        file_content = ContentFile(b"Test content here", name="test.txt")
        managed_file = ManagedFile.objects.create(
            file=file_content,
            owner=self.user
        )
        self.assertGreater(managed_file.file_size, 0)

    def test_file_with_folder(self):
        """Test creating a file in a folder"""
        folder = Folder.objects.create(name="Documents", owner=self.user)
        file_content = ContentFile(b"Test", name="test.txt")
        managed_file = ManagedFile.objects.create(
            file=file_content,
            folder=folder,
            owner=self.user,
            file_size=10
        )
        self.assertEqual(managed_file.folder, folder)


@override_settings(TIKA_SERVER_ENDPOINT='https://tika-service.captain.statezero.dev')
class TikaParserTest(TestCase):
    """Test TikaParser with live service"""

    def test_parse_text_file(self):
        """Test parsing a simple text file"""
        # Create temporary text file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("Hello, this is a test document.\n")
            f.write("It has multiple lines.\n")
            f.write("Testing OCR functionality.")
            temp_path = f.name

        try:
            parser = TikaParser()
            content, metadata = parser.parse(temp_path)

            self.assertIn("test document", content)
            self.assertIn("multiple lines", content)
            self.assertIsInstance(metadata, dict)
        finally:
            os.unlink(temp_path)

    def test_parse_bytes(self):
        """Test parsing from bytes"""
        content_bytes = b"This is a test file content"
        parser = TikaParser()
        content, metadata = parser.parse(content_bytes)

        self.assertIn("test file content", content)

    def test_parser_exception_on_invalid_file(self):
        """Test that parser raises OCRException on failure"""
        parser = TikaParser()
        with self.assertRaises(OCRException):
            parser.parse("/nonexistent/file.pdf")

    def test_get_parser_class(self):
        """Test dynamic parser loading"""
        parser = get_parser_class('django_ai.files.parsers.TikaParser')
        self.assertIsInstance(parser, TikaParser)


@override_settings(
    TIKA_SERVER_ENDPOINT='https://tika-service.captain.statezero.dev',
    FILE_PARSER_CLASS='django_ai.files.parsers.TikaParser'
)
class OCRProcessingTest(TestCase):
    """End-to-end OCR processing tests"""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def test_ocr_processing_text_file(self):
        """Test complete OCR processing workflow"""
        # Create a text file
        file_content = ContentFile(
            b"This is a sample document for OCR testing.\nIt contains important information.",
            name="sample.txt"
        )

        managed_file = ManagedFile.objects.create(
            file=file_content,
            owner=self.user,
            mime_type="text/plain"
        )

        # Process OCR
        result = process_file_ocr(managed_file.id)

        # Verify result
        self.assertEqual(result['status'], 'success')
        self.assertIn('processing_time_ms', result)
        self.assertGreater(result['content_length'], 0)

        # Verify database records
        managed_file.refresh_from_db()
        self.assertEqual(managed_file.ocr_status, 'completed')

        # Verify OCR output
        ocr_output = managed_file.ocr_outputs.first()
        self.assertIsNotNone(ocr_output)
        self.assertTrue(ocr_output.success)
        self.assertIn("sample document", ocr_output.content)
        self.assertIn("OCR testing", ocr_output.content)

    def test_get_file_content_tool(self):
        """Test the get_file_content tool"""
        # Create and process a file
        file_content = ContentFile(
            b"Line 1: First line\nLine 2: Second line\nLine 3: Third line",
            name="test.txt"
        )

        managed_file = ManagedFile.objects.create(
            file=file_content,
            owner=self.user
        )

        # Process OCR
        process_file_ocr(managed_file.id)

        # Test getting full content
        content = get_file_content(managed_file.id)
        self.assertIn("First line", content)
        self.assertIn("Second line", content)

        # Test getting partial content with slicing
        partial = get_file_content(managed_file.id, start=0, stop=20)
        self.assertLessEqual(len(partial), 20)

    def test_ocr_with_folder(self):
        """Test OCR processing for file in a folder"""
        folder = Folder.objects.create(name="Test Folder", owner=self.user)

        file_content = ContentFile(
            b"Document in a folder",
            name="folder_doc.txt"
        )

        managed_file = ManagedFile.objects.create(
            file=file_content,
            folder=folder,
            owner=self.user
        )

        result = process_file_ocr(managed_file.id)
        self.assertEqual(result['status'], 'success')

        managed_file.refresh_from_db()
        self.assertEqual(managed_file.ocr_status, 'completed')
        self.assertEqual(managed_file.folder.name, "Test Folder")

    def test_ocr_failure_handling(self):
        """Test that OCR failures are handled gracefully"""
        # Create file with invalid path (will fail to parse)
        managed_file = ManagedFile.objects.create(
            file="invalid/path/file.pdf",
            filename="nonexistent.pdf",
            owner=self.user,
            file_size=100
        )

        result = process_file_ocr(managed_file.id)

        # Should return failure status
        self.assertEqual(result['status'], 'failed')
        self.assertIn('error', result)

        # File status should be failed
        managed_file.refresh_from_db()
        self.assertEqual(managed_file.ocr_status, 'failed')

        # Should have error OCR output
        ocr_output = managed_file.ocr_outputs.first()
        self.assertIsNotNone(ocr_output)
        self.assertFalse(ocr_output.success)
        self.assertNotEqual(ocr_output.error_message, '')


class OCROutputModelTest(TestCase):
    """Test OCROutput model"""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')
        file_content = ContentFile(b"Test", name="test.txt")
        self.managed_file = ManagedFile.objects.create(
            file=file_content,
            owner=self.user,
            file_size=10
        )

    def test_create_ocr_output(self):
        """Test creating OCR output"""
        ocr = OCROutput.objects.create(
            managed_file=self.managed_file,
            content="Extracted text content",
            parser_class="django_ai.files.parsers.TikaParser",
            parser_version="1.0",
            success=True
        )
        self.assertEqual(ocr.content, "Extracted text content")
        self.assertTrue(ocr.success)

    def test_failed_ocr_output(self):
        """Test creating failed OCR output"""
        ocr = OCROutput.objects.create(
            managed_file=self.managed_file,
            content="",
            parser_class="django_ai.files.parsers.TikaParser",
            parser_version="1.0",
            success=False,
            error_message="Parser failed"
        )
        self.assertFalse(ocr.success)
        self.assertEqual(ocr.error_message, "Parser failed")
        self.assertEqual(ocr.content, "")

    def test_multiple_ocr_outputs(self):
        """Test that a file can have multiple OCR attempts"""
        # First attempt - failed
        OCROutput.objects.create(
            managed_file=self.managed_file,
            content="",
            parser_class="django_ai.files.parsers.TikaParser",
            success=False,
            error_message="First attempt failed"
        )

        # Second attempt - success
        OCROutput.objects.create(
            managed_file=self.managed_file,
            content="Successfully extracted",
            parser_class="django_ai.files.parsers.TikaParser",
            success=True
        )

        self.assertEqual(self.managed_file.ocr_outputs.count(), 2)
        latest_success = self.managed_file.ocr_outputs.filter(success=True).first()
        self.assertEqual(latest_success.content, "Successfully extracted")
