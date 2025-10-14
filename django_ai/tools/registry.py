"""
Tool registry with decorator for agent tools.
Preserves function signatures and docstrings for LLM understanding.
"""
import functools
import inspect
from typing import Callable, Dict, Optional, List


# Global tool registry
_tool_registry: Dict[str, Dict] = {}


def register_tool(
    title: Optional[str] = None,
    description: Optional[str] = None,
    icon: str = "",
    category: str = "",
    use_cases: Optional[List[str]] = None
):
    """
    Decorator to register a function as a tool.
    Preserves function signature and docstring for LLM.

    Auto mode: If title/description not provided, auto-generates from function.
    - title: snake_case -> Title Case (e.g., "get_file_content" -> "Get File Content")
    - description: First line of docstring

    Args:
        title: User-facing name (auto-generated if not provided)
        description: Tool description (uses docstring if not provided)
        icon: Icon identifier (e.g., 'file', 'database', 'search')
        category: Category for grouping (e.g., 'files', 'database', 'web')
        use_cases: List of use case strings for semantic retrieval/search

    Examples:
        # Minimal - auto mode
        @register_tool(icon="file", category="files")
        def get_file_content(file_id: int) -> str:
            '''Get OCR-extracted content from a file.'''
            ...

        # With use cases
        @register_tool(
            icon="table",
            category="data",
            use_cases=["query data", "filter rows", "search database"]
        )
        def execute_code(code: str) -> dict:
            '''Execute Python code with preloaded table(s).'''
            ...

        # Full specification
        @register_tool(
            title="Search Knowledge Base",
            description="Semantic search across documentation",
            icon="search",
            category="knowledge",
            use_cases=["find docs", "search help"]
        )
        def search_kb(query: str) -> list:
            ...
    """
    def decorator(func: Callable) -> Callable:
        tool_name = func.__name__
        module_path = func.__module__

        # Auto-generate title: "get_file_content" -> "Get File Content"
        auto_title = tool_name.replace("_", " ").title()

        # Auto-generate description from docstring first line
        auto_description = ""
        if func.__doc__:
            auto_description = func.__doc__.strip().split("\n")[0]

        # Store tool metadata
        _tool_registry[tool_name] = {
            "name": tool_name,
            "module": module_path,  # Store module to avoid namespace conflicts
            "title": title if title is not None else auto_title,
            "description": description if description is not None else auto_description,
            "icon": icon,
            "category": category,
            "use_cases": use_cases or [],
            "func": func,
            "signature": inspect.signature(func),
            "docstring": func.__doc__ or "",
        }

        # Return function unchanged - preserves signature, docstring, etc.
        return func

    return decorator


def get_tool(name: str) -> Callable:
    """
    Get a tool function by name.

    Args:
        name: Tool name (function name)

    Returns:
        The callable tool function

    Raises:
        ValueError: If tool not found in registry
    """
    if name not in _tool_registry:
        available = ", ".join(_tool_registry.keys())
        raise ValueError(
            f"Tool '{name}' not registered. "
            f"Available tools: {available or 'none'}"
        )
    return _tool_registry[name]["func"]


def get_tool_metadata(name: str) -> Dict:
    """
    Get full tool metadata including signature, docstring, etc.

    Args:
        name: Tool name (function name)

    Returns:
        Dictionary with tool metadata

    Raises:
        ValueError: If tool not found in registry
    """
    if name not in _tool_registry:
        available = ", ".join(_tool_registry.keys())
        raise ValueError(
            f"Tool '{name}' not registered. "
            f"Available tools: {available or 'none'}"
        )
    return _tool_registry[name].copy()


def list_tools(category: Optional[str] = None) -> Dict[str, Dict]:
    """
    List all registered tools, optionally filtered by category.

    Args:
        category: Optional category to filter by

    Returns:
        Dictionary mapping tool names to their metadata
    """
    if category is None:
        return _tool_registry.copy()

    return {
        name: metadata
        for name, metadata in _tool_registry.items()
        if metadata["category"] == category
    }


def tool_exists(name: str) -> bool:
    """Check if a tool is registered."""
    return name in _tool_registry


def normalize_tool_reference(tool_ref) -> str:
    """
    Normalize a tool reference to a tool name.

    Args:
        tool_ref: Either a string (tool name) or a callable (the tool function)

    Returns:
        The tool name as a string

    Raises:
        ValueError: If tool_ref is a function but not registered

    Examples:
        normalize_tool_reference("get_file_content")  # "get_file_content"
        normalize_tool_reference(get_file_content)    # "get_file_content"
    """
    # If it's already a string, return it
    if isinstance(tool_ref, str):
        return tool_ref

    # If it's a callable, get its name
    if callable(tool_ref):
        tool_name = tool_ref.__name__

        # Verify it's actually registered and is the same function
        if tool_name not in _tool_registry:
            raise ValueError(
                f"Function '{tool_name}' is not registered as a tool. "
                f"Use @register_tool decorator to register it first."
            )

        registered_func = _tool_registry[tool_name]["func"]
        if registered_func is not tool_ref:
            raise ValueError(
                f"Function '{tool_name}' exists in registry but is a different function. "
                f"Possible namespace conflict. Registered from module: "
                f"{_tool_registry[tool_name]['module']}"
            )

        return tool_name

    raise ValueError(
        f"Tool reference must be a string or callable, got {type(tool_ref)}"
    )
