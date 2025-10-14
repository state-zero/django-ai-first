# Dynamic Tool System for Agents

## Overview

The tool system allows agents to have:
- **base_tools**: Fixed tools that every instance of an agent type has
- **additional_tools**: Per-session tools that can be customized
- **available_tools**: Computed property = base_tools + additional_tools

## Registering Tools

Tools are registered with the `@register_tool` decorator, which preserves function signatures and docstrings for LLM understanding.

```python
from django_ai.tools import register_tool

# Auto mode - title and description auto-generated
@register_tool(icon="file", category="files")
def get_file_content(file_id: int, start: int = None) -> str:
    """Get the OCR-extracted content from a managed file."""
    # Auto title: "Get File Content"
    # Auto description: "Get the OCR-extracted content from a managed file."
    ...

# With use cases for semantic retrieval
@register_tool(
    icon="table",
    category="data",
    use_cases=["query data", "filter rows", "search database"]
)
def execute_code(code: str, table=None) -> dict:
    """Execute Python code with preloaded table(s)."""
    ...
```

## Defining Agents with Tools

```python
from django_ai.conversations.base import ConversationAgent
from pydantic import BaseModel

class DataAnalystAgent(ConversationAgent):
    class Tools:
        base = ["get_metadata", "execute_code"]  # Base tools - always available
        available = ["revert_version", "get_file_content"]  # Tools that can be added per-session
        max_additional = 3  # Limit per session (default: 5, None = unlimited)

    class Context(BaseModel):
        user_id: int
        table_id: int = None

    @classmethod
    def create_context(cls, request=None, **kwargs):
        return cls.Context(**kwargs)

    async def get_response(self, message, request=None, **kwargs):
        # Get available tools (base + additional from session)
        tools = self.tools

        # Tools have preserved signatures and docstrings!
        # Pass to LLM...
        response = await llm.generate(message, tools=tools)
        return response
```

## Using Per-Session Tool Customization

```python
from django_ai.conversations.models import ConversationSession, SessionToolLoadout
from django_ai.tools.models import ToolLoadout
from django_ai.tools import get_tool_metadata
from django_ai.conversations.registry import registry

# Create a session
session = ConversationSession.objects.create(
    agent_path="data_analyst",
    user=request.user,
    context=DataAnalystAgent.create_context(user_id=123, table_id=456)
)

# Get agent class for tool info
agent_class = registry.get("data_analyst")

# Create a ToolLoadout with additional tools
tool_loadout = ToolLoadout.objects.create(
    additional_tools=["revert_version"]
)

# Validate it's valid for this agent
tool_loadout.validate_for_agent(agent_class)  # Raises ValidationError if invalid

# Link the loadout to the session
SessionToolLoadout.objects.create(
    session=session,
    loadout=tool_loadout
)

# Get available tools for UI display
available_tools = agent_class.Tools.available  # ["revert_version", "get_file_content"]
max_additional = agent_class.Tools.max_additional  # 3

# Display tools in UI
for tool_name in available_tools:
    metadata = get_tool_metadata(tool_name)
    print(f"{metadata['title']}: {metadata['description']}")

# Now agent has: base tools + additional tools from loadout
# ["get_metadata", "execute_code", "revert_version"]
```

## Tool Auto-Generation

- **Title**: `get_file_content` → "Get File Content"
- **Description**: First line of docstring
- **Name**: Always the function name (stable identifier)

## Validation

- Agent `Tools` configuration is validated on agent initialization:
  - Only allowed attributes: `base`, `available`, `max_additional`
  - `available` cannot contain tools from `base` (no duplicates)
  - All tools in `base` exist in the registry
- `SessionToolLoadout.additional_tools` are validated on save:
  - Tools exist in the registry
  - Tools are in the agent's `Tools.available` list
  - Number of additional tools doesn't exceed `Tools.max_additional`

## Current Registered Tools

### Files (category: "files")
- `get_file_content`: Read OCR-extracted content from files

### Data (category: "data")
- `get_metadata`: View table/workspace structure and schema
- `execute_code`: Run Python code with preloaded tables
- `revert_version`: Undo table changes and restore previous versions
