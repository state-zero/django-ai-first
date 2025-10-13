"""
Tool functions for LLM agents to interact with tables and workspaces.
"""

from typing import Optional
from .models import Table, Workspace


class TableToolError(Exception):
    """Base exception for table tool errors with LLM-friendly messages"""
    pass


class InvalidParameterError(TableToolError):
    """Raised when tool parameters are invalid"""
    pass


class ExecutionError(TableToolError):
    """Raised when code execution fails"""
    pass


class VersionError(TableToolError):
    """Raised when version operations fail"""
    pass


def _validate_table_or_workspace(table: Optional[Table], workspace: Optional[Workspace]) -> None:
    """Validate that exactly one of table or workspace is provided"""
    if table is not None and workspace is not None:
        raise InvalidParameterError(
            "You must provide EITHER a table OR a workspace, not both. "
            "Please call this function again with only one parameter."
        )
    if table is None and workspace is None:
        raise InvalidParameterError(
            "You must provide either a table or a workspace. "
            "Please call this function again with one of these parameters."
        )


def get_metadata(
    table: Optional[Table] = None,
    workspace: Optional[Workspace] = None,
    head: Optional[int] = None,
    tail: Optional[int] = None,
    include_history: bool = True
) -> dict:
    """
    Get metadata about a table or workspace.

    Args:
        table: A Table instance (mutually exclusive with workspace)
        workspace: A Workspace instance (mutually exclusive with table)
        head: Number of rows from the beginning to include in preview
        tail: Number of rows from the end to include in preview
        include_history: Whether to include version history

    Returns:
        dict with metadata

    Raises:
        InvalidParameterError: If both or neither table/workspace are provided
    """
    _validate_table_or_workspace(table, workspace)

    if table is not None:
        return table.get_metadata(head=head, tail=tail, include_history=include_history)
    else:
        return workspace.get_metadata(head=head, tail=tail, include_history=include_history)


def execute_code(
    code: str,
    table: Optional[Table] = None,
    workspace: Optional[Workspace] = None,
    e2b_api_key: Optional[str] = None,
    auto_persist: bool = True
) -> dict:
    """
    Execute Python code with preloaded table(s) in a remote sandbox.

    For a single table, use variable name 'table'.
    For workspace, use variable names 'table_1', 'table_2', etc.

    Args:
        code: Python code to execute
        table: A Table instance (mutually exclusive with workspace)
        workspace: A Workspace instance (mutually exclusive with table)
        e2b_api_key: E2B API key for sandbox execution (optional)
        auto_persist: If True, automatically save modified tables

    Returns:
        dict with execution results

    Raises:
        InvalidParameterError: If both or neither table/workspace are provided
        ExecutionError: If code execution fails
    """
    _validate_table_or_workspace(table, workspace)

    try:
        if table is not None:
            result = table.run_script(script=code, e2b_api_key=e2b_api_key, auto_persist=auto_persist)
        else:
            result = workspace.run_script(script=code, e2b_api_key=e2b_api_key, auto_persist=auto_persist)

        if result.get('error'):
            raise ExecutionError(
                f"Code execution failed: {result['error']}\n"
                f"Stderr: {result.get('stderr', '')}\n"
                "Please fix the error and try again."
            )

        return result

    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Unexpected error during code execution: {str(e)}")


def revert_version(table: Table, history_id: Optional[int] = None) -> dict:
    """
    Revert a table to a previous version.

    Args:
        table: The Table instance to revert
        history_id: Specific history record ID to revert to (optional)

    Returns:
        dict with revert information

    Raises:
        VersionError: If revert operation fails
    """
    try:
        previous_version = table.version

        # Check if history exists
        history_count = table.history.count()
        if history_count < 2:
            raise VersionError(
                "Cannot revert: This table has no previous versions. "
                "A table needs at least 2 history records to revert."
            )

        # Validate history_id if provided
        if history_id is not None:
            if not table.history.filter(history_id=history_id).exists():
                available = list(table.history.values_list('history_id', flat=True))
                raise VersionError(
                    f"Cannot revert: History record {history_id} does not exist. "
                    f"Available history IDs: {available}"
                )

        # Perform revert
        table.revert_to_version(history_id=history_id)
        table.refresh_from_db()

        return {
            'success': True,
            'previous_version': previous_version,
            'new_version': table.version,
            'message': f"Reverted table from version {previous_version} to version {table.version}"
        }

    except VersionError:
        raise
    except Exception as e:
        raise VersionError(f"Unexpected error during version revert: {str(e)}")
