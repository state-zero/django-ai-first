# Django AI Files Module

This module provides file management with automatic OCR/text extraction capabilities for django-ai-first.

## Features

- **Folder Management**: Hierarchical folder structure for organizing files
- **File Upload & Storage**: Store and manage files with metadata
- **Automatic OCR Processing**: Background processing using django-q2 queues
- **Parser Strategy Pattern**: Pluggable parsers (Tika, plain text, etc.)
- **Agent Tools**: Ready-to-use functions for AI agents to interact with files

## Models

### Folder
Virtual folders for organizing files in a hierarchical structure.

### ManagedFile
Represents uploaded files with automatic OCR processing on creation.

### OCROutput
Stores the extracted text content and metadata from file parsing.

## Configuration

Add to your Django settings:

```python
INSTALLED_APPS = [
    # ...
    'django_ai.files',
]

# Optional: Customize the file parser (defaults to TikaParser)
FILE_PARSER_CLASS = 'django_ai.files.parsers.TikaParser'

# Optional: Configure Tika server endpoint
# If not set, uses local Tika or TIKA_SERVER_ENDPOINT environment variable
TIKA_SERVER_ENDPOINT = 'http://localhost:9998'

# Optional: Configure file upload path
MEDIA_ROOT = BASE_DIR / 'media'
MEDIA_URL = '/media/'
```

## Available Parsers

### TikaParser
Uses Apache Tika for comprehensive document parsing including:
- PDFs (with OCR support via Tesseract)
- Microsoft Office documents (Word, Excel, PowerPoint)
- Images (PNG, JPEG, TIFF)
- Plain text and HTML

Requires: `tika` package (already in requirements.txt)

### Custom Parsers
Create your own parser by extending `BaseParser`:

```python
from django_ai.files.parsers import BaseParser

class MyCustomParser(BaseParser):
    def parse(self, file_source):
        # Your parsing logic
        content = "..."
        metadata = {}
        return content, metadata

    def get_version(self):
        return "1.0.0"

    def get_supported_mime_types(self):
        return ['application/custom']
```

Then set in settings:
```python
FILE_PARSER_CLASS = 'myapp.parsers.MyCustomParser'
```

## Agent Tools

The module provides ready-to-use tools for AI agents:

### get_file_content(file_id, start=None, stop=None)
Get OCR-extracted content from a file with optional slicing.

```python
from django_ai.files.tools import get_file_content

# Get full content
content = get_file_content(file_id=123)

# Get partial content (first 1000 characters)
content = get_file_content(file_id=123, start=0, stop=1000)

# Get content from position 5000 onwards
content = get_file_content(file_id=123, start=5000)
```

### get_file_metadata(file_id)
Get comprehensive metadata about a file.

### list_files(folder_id=None, owner_id=None, mime_type=None, limit=50)
List files with optional filtering.

### search_file_content(query, folder_id=None, owner_id=None, limit=10)
Search for text within file contents.

### list_folders(parent_id=None, owner_id=None)
List folders with optional filtering.

### get_file_summary(file_id, max_chars=1000)
Get a formatted summary including metadata and content preview.

## Usage Example

```python
from django.contrib.auth import get_user_model
from django_ai.files.models import Folder, ManagedFile

User = get_user_model()
user = User.objects.first()

# Create a folder
folder = Folder.objects.create(
    name="Project Documents",
    owner=user
)

# Upload a file (OCR processing triggers automatically)
from django.core.files import File
with open('document.pdf', 'rb') as f:
    managed_file = ManagedFile.objects.create(
        file=File(f, name='document.pdf'),
        filename='document.pdf',
        folder=folder,
        owner=user,
        file_size=f.seek(0, 2),  # Get file size
        mime_type='application/pdf'
    )

# Wait for OCR processing (happens in background queue)
# Then access the content via agent tools
from django_ai.files.tools import get_file_content, get_file_metadata

content = get_file_content(managed_file.id)
metadata = get_file_metadata(managed_file.id)
```

## Background Processing

Files are automatically queued for OCR processing when created. The queue task `process_file_ocr` runs in the background using django-q2.

### Manual Reprocessing

```python
from django_q.tasks import async_task

# Reprocess a specific file
async_task('django_ai.files.tasks.process_file_ocr', file_id=123)

# Reprocess failed files older than 24 hours
async_task('django_ai.files.tasks.reprocess_failed_ocr', hours_old=24)
```

## Signals

The module uses Django signals to trigger OCR processing:

- `post_save` on `ManagedFile`: Automatically queues OCR processing for new files

## Testing

```bash
python manage.py test django_ai.files
```
