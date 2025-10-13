from django.db import models
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.conf import settings

from simple_history.models import HistoricalRecords
import pandas as pd
import io
import uuid
import os

User = get_user_model()


def table_upload_path(instance, filename):
    """Generate upload path organized by table ID for easy cleanup"""
    return f'tables/{instance.id}/{filename}'


class Table(models.Model):
    """A table stored as a parquet file with versioning"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, help_text="Descriptive name for the table")

    # Parquet file storage
    file = models.FileField(upload_to=table_upload_path)

    # Auto-incrementing version
    version = models.PositiveIntegerField(default=1)

    # Metadata
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='tables'
    )
    description = models.TextField(blank=True)
    schema_info = models.JSONField(
        default=dict,
        blank=True,
        help_text="Column names, dtypes, and other schema information"
    )
    row_count = models.PositiveIntegerField(default=0)

    # Track who/what updated the table
    updated_role = models.CharField(
        max_length=20,
        choices=[
            ('user', 'User'),
            ('assistant', 'Assistant'),
        ],
        default='user',
        help_text="Whether this update was made by a user or assistant"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Historical tracking
    history = HistoricalRecords()

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['owner', '-updated_at']),
            models.Index(fields=['name']),
        ]

    def __str__(self):
        return f"{self.name} (v{self.version})"

    def delete(self, *args, **kwargs):
        """Delete the table and all its version files"""
        if self.file:
            storage = self.file.storage
            folder_path = f'tables/{self.id}/'
            try:
                # List and delete all files in the table's folder
                directories, files = storage.listdir(folder_path)
                for filename in files:
                    file_path = f'{folder_path}{filename}'
                    storage.delete(file_path)
                # Note: Some storage backends may need additional cleanup for directories
            except Exception:
                pass  # Folder might not exist or storage doesn't support listdir
        super().delete(*args, **kwargs)

    def load_dataframe(self) -> pd.DataFrame:
        """Load the parquet file into a pandas DataFrame"""
        with self.file.open('rb') as f:
            return pd.read_parquet(f)

    def save_dataframe(self, df: pd.DataFrame, increment_version: bool = True, updated_role: str = 'user'):
        """Save a pandas DataFrame as a parquet file"""
        # Increment version if requested
        if increment_version and self.pk:
            self.version += 1

        # Convert DataFrame to parquet bytes
        buffer = io.BytesIO()
        df.to_parquet(buffer, index=False)
        buffer.seek(0)

        # Save to file field
        filename = f"{self.name}_v{self.version}.parquet"
        self.file.save(filename, ContentFile(buffer.read()), save=False)

        # Update metadata
        self.row_count = len(df)
        self.schema_info = {
            'columns': list(df.columns),
            'dtypes': {col: str(dtype) for col, dtype in df.dtypes.items()},
        }
        self.updated_role = updated_role

        self.save()

    def get_metadata(self, head: int = None, tail: int = None, include_history: bool = True) -> dict:
        """
        Get comprehensive metadata about the table.

        Args:
            head: Number of rows from the beginning to include in preview
            tail: Number of rows from the end to include in preview
            include_history: Whether to include version history

        Returns:
            dict with table metadata including optional preview rows and history
        """
        metadata = {
            'id': str(self.id),
            'name': self.name,
            'description': self.description,
            'version': self.version,
            'row_count': self.row_count,
            'columns': self.schema_info.get('columns', []),
            'dtypes': self.schema_info.get('dtypes', {}),
            'updated_role': self.updated_role,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
        }

        # Add preview rows if requested
        if head is not None or tail is not None:
            df = self.load_dataframe()
            if head is not None and head > 0:
                metadata['head_rows'] = df.head(head).to_dict(orient='records')
            if tail is not None and tail > 0:
                metadata['tail_rows'] = df.tail(tail).to_dict(orient='records')

        # Add history if requested
        if include_history:
            metadata['history'] = [
                {
                    'history_id': h.history_id,
                    'version': h.version,
                    'updated_role': h.updated_role,
                    'updated_at': h.history_date.isoformat(),
                }
                for h in self.history.all()
            ]

        return metadata

    def revert_to_version(self, history_id: int = None):
        """
        Revert table to a previous version from history by loading the old file content.

        Args:
            history_id: Specific history record ID to revert to. If None, reverts to previous version.
        """
        if history_id:
            historical_record = self.history.get(history_id=history_id)
        else:
            # Get the previous version (skip the most recent one)
            historical_record = self.history.all()[1]

        # Load the data from the historical file using storage backend
        storage = self.file.storage
        with storage.open(historical_record.file, 'rb') as f:
            df = pd.read_parquet(f)

        # Save as a new version with the old data (marks as user action by default)
        self.save_dataframe(df, increment_version=True, updated_role='user')

    def run_script(self, script: str, e2b_api_key: str = None, auto_persist: bool = True) -> dict:
        """
        Execute a Python script with this table preloaded as 'table'.
        Table data is transferred to/from the sandbox as JSON.
        After execution, the modified table is automatically persisted.

        Args:
            script: Python code to execute
            e2b_api_key: E2B API key (optional, falls back to settings.E2B_API_KEY or env var)
            auto_persist: If True, automatically save modified table back to storage

        Returns:
            dict with 'stdout', 'stderr', 'error', 'results', and 'persisted'
        """
        try:
            from e2b_code_interpreter import Sandbox
        except ImportError:
            raise ImportError(
                "e2b-code-interpreter is required for code execution. "
                "Install it with: pip install e2b-code-interpreter"
            )

        # Create sandbox (reads E2B_API_KEY from environment)
        sandbox = Sandbox.create()

        try:
            # Upload table as JSON (safe, portable, no dependencies)
            remote_path = "/tmp/table.json"
            df = self.load_dataframe()
            json_str = df.to_json(orient='records')
            sandbox.files.write(remote_path, json_str.encode('utf-8'))

            # Build the complete script with preloaded table and persistence code
            preload_script = "\n".join([
                "import pandas as pd",
                "import numpy as np",
                "",
                "# Preloaded table",
                f"table = pd.read_json('{remote_path}', orient='records')",
                "",
                "# User script",
                script,
                "",
                "# Persist final state of table",
                "table.to_json('/tmp/table_final.json', orient='records')",
            ])

            # Execute the script
            execution = sandbox.run_code(preload_script)

            result = {
                'stdout': execution.logs.stdout,
                'stderr': execution.logs.stderr,
                'error': execution.error,
                'results': execution.results,
                'persisted': False
            }

            # Download and persist modified table
            if auto_persist and not execution.error:
                try:
                    # Download the final state of the table
                    final_json_data = sandbox.files.read('/tmp/table_final.json')

                    # Load into DataFrame and save (mark as assistant update)
                    if isinstance(final_json_data, bytes):
                        df = pd.read_json(io.BytesIO(final_json_data), orient='records')
                    else:
                        df = pd.read_json(io.StringIO(final_json_data), orient='records')
                    self.save_dataframe(df, increment_version=True, updated_role='assistant')
                    result['persisted'] = True
                    result['new_version'] = self.version
                except Exception as persist_error:
                    result['persist_error'] = str(persist_error)

            return result

        finally:
            sandbox.kill()

    @classmethod
    def create_from_dataframe(cls, name: str, df: pd.DataFrame, owner, description: str = ""):
        """Create a new Table from a pandas DataFrame"""
        table = cls(name=name, owner=owner, description=description)
        table.save_dataframe(df, increment_version=False)
        return table


class Workspace(models.Model):
    """A collection of tables for data analysis and code execution"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='workspaces'
    )

    tables = models.ManyToManyField(
        Table,
        related_name='workspaces',
        blank=True
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['owner', '-updated_at']),
        ]

    def __str__(self):
        return self.name

    def get_table_context(self) -> dict:
        """
        Get a dictionary mapping variable names to DataFrames.
        Variables are named as 'table_1', 'table_2', etc.
        """
        context = {}
        for i, table in enumerate(self.tables.all(), start=1):
            var_name = f"table_{i}"
            context[var_name] = table.load_dataframe()
        return context

    def get_metadata(self, head: int = None, tail: int = None, include_history: bool = True) -> dict:
        """
        Get comprehensive metadata about the workspace and all its tables.

        Args:
            head: Number of rows from the beginning to include in preview for each table
            tail: Number of rows from the end to include in preview for each table
            include_history: Whether to include version history for each table

        Returns:
            dict with workspace metadata and table metadata
        """
        workspace_metadata = {
            'workspace_id': str(self.id),
            'workspace_name': self.name,
            'workspace_description': self.description,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'tables': {}
        }

        for i, table in enumerate(self.tables.all(), start=1):
            var_name = f"table_{i}"
            table_metadata = table.get_metadata(head=head, tail=tail, include_history=include_history)
            table_metadata['variable_name'] = var_name
            workspace_metadata['tables'][var_name] = table_metadata

        return workspace_metadata

    def run_script(self, script: str, e2b_api_key: str = None, auto_persist: bool = True) -> dict:
        """
        Execute a Python script with preloaded tables in E2B sandbox.
        Table data is transferred to/from the sandbox as JSON.
        After execution, modified tables are automatically persisted.

        Args:
            script: Python code to execute
            e2b_api_key: E2B API key (optional, falls back to settings.E2B_API_KEY or env var)
            auto_persist: If True, automatically save modified tables back to storage

        Returns:
            dict with 'stdout', 'stderr', 'error', 'results', and 'persisted_tables'
        """
        try:
            from e2b_code_interpreter import Sandbox
        except ImportError:
            raise ImportError(
                "e2b-code-interpreter is required for code execution. "
                "Install it with: pip install e2b-code-interpreter"
            )

        # Create sandbox (reads E2B_API_KEY from environment)
        sandbox = Sandbox.create()

        try:
            # Upload tables as JSON (safe, portable, no dependencies)
            table_mapping = {}  # Maps var_name to Table instance
            table_vars = []
            for i, table in enumerate(self.tables.all(), start=1):
                var_name = f"table_{i}"
                table_mapping[var_name] = table

                # Upload the table as JSON
                remote_path = f"/tmp/{var_name}.json"
                df = table.load_dataframe()
                json_str = df.to_json(orient='records')
                sandbox.files.write(remote_path, json_str.encode('utf-8'))

                table_vars.append(f"{var_name} = pd.read_json('{remote_path}', orient='records')")

            # Build the complete script with preloaded tables and persistence code
            persist_code = []
            for var_name in table_mapping.keys():
                persist_code.append(f"{var_name}.to_json('/tmp/{var_name}_final.json', orient='records')")

            preload_script = "\n".join([
                "import pandas as pd",
                "import numpy as np",
                "",
                "# Preloaded tables",
                *table_vars,
                "",
                "# User script",
                script,
                "",
                "# Persist final state of tables",
                *persist_code,
            ])

            # Execute the script
            execution = sandbox.run_code(preload_script)

            result = {
                'stdout': execution.logs.stdout,
                'stderr': execution.logs.stderr,
                'error': execution.error,
                'results': execution.results,
                'persisted_tables': []
            }

            # Download and persist modified tables
            if auto_persist and not execution.error:
                for var_name, table in table_mapping.items():
                    try:
                        # Download the final state of the table
                        final_json_path = f"/tmp/{var_name}_final.json"
                        final_json_data = sandbox.files.read(final_json_path)

                        # Load into DataFrame and save (mark as assistant update)
                        if isinstance(final_json_data, bytes):
                            df = pd.read_json(io.BytesIO(final_json_data), orient='records')
                        else:
                            df = pd.read_json(io.StringIO(final_json_data), orient='records')
                        table.save_dataframe(df, increment_version=True, updated_role='assistant')
                        result['persisted_tables'].append({
                            'variable': var_name,
                            'table_id': str(table.id),
                            'table_name': table.name,
                            'new_version': table.version,
                        })
                    except Exception as persist_error:
                        result['persisted_tables'].append({
                            'variable': var_name,
                            'table_id': str(table.id),
                            'error': str(persist_error),
                        })

            return result

        finally:
            sandbox.kill()
