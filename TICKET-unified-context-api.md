# Unified Context API

## Summary

Unify context access across workflows, agents, and conversations to a single pattern: `self.context`

## The API

### Access: `self.context`

```python
# Workflows
@step(start=True)
def start(self):
    self.context.status = "started"

# Event Agents
@handler("message_received")
def handle(self, events):
    self.context.processed = True

# Conversation Agents
def get_response(self, message, **kwargs):
    self.context.last_message = message

# Tools
@tool
def search(self, query: str):
    return db.query(self.context.dataset_id, query)
```

### Definition: `class Context(BaseModel)`

```python
class MyWorkflow:
    class Context(BaseModel):
        reservation_id: int
        status: str = "pending"

class MyAgent:
    class Context(BaseModel):
        user_id: int
        messages_processed: int = 0
```

### Creation: `create_context()` classmethod

```python
@classmethod
def create_context(cls, event=None, request=None, **kwargs):
    return cls.Context(...)
```

### Persistence

Framework handles loading/saving automatically:
- **Workflows**: `WorkflowRun.data`
- **Event Agents**: `AgentRun.context`
- **Conversations**: `ConversationSession.context`

## Implementation

### Single Contextvar

```python
# django_ai/context.py

from contextvars import ContextVar
from pydantic import BaseModel

_current_context: ContextVar[BaseModel] = ContextVar("context", default=None)


def set_context(ctx: BaseModel):
    """Set the current context. Called by framework."""
    return _current_context.set(ctx)


def reset_context(token):
    """Reset context to previous value. Called by framework."""
    _current_context.reset(token)


def get_current_context() -> BaseModel:
    """Get current context. Used internally by ContextMixin."""
    return _current_context.get()
```

### ContextMixin

```python
# django_ai/context.py

class ContextMixin:
    """
    Mixin providing self.context access.

    Include in workflows, agents, conversations.
    """

    @property
    def context(self) -> BaseModel:
        ctx = get_current_context()
        if ctx is None:
            raise RuntimeError(
                "No context available. "
                "Are you inside a workflow step, agent handler, or conversation?"
            )
        return ctx
```

### Framework Integration

Each execution point sets context before calling user code:

```python
# Workflow engine
def execute_step(workflow_instance, step_method, context):
    token = set_context(context)
    try:
        result = step_method()
    finally:
        reset_context(token)
        save_context(context)  # Persist changes
    return result

# Agent handler execution
def execute_handler(agent_instance, handler_method, context, events):
    token = set_context(context)
    try:
        result = handler_method(events)
    finally:
        reset_context(token)
        save_context(context)
    return result

# Conversation service
def call_get_response(agent_instance, message, context):
    token = set_context(context)
    try:
        result = agent_instance.get_response(message)
    finally:
        reset_context(token)
        save_context(context)
    return result
```

## Changes Required

### 1. Create `django_ai/context.py`
- `_current_context` contextvar
- `set_context()`, `reset_context()`, `get_current_context()`
- `ContextMixin` class

### 2. Update Workflows (`django_ai/automation/workflows/core.py`)
- Add `ContextMixin` to workflow base
- Update engine to use `set_context()` / `reset_context()`
- Remove `get_context()` function (or keep as alias)

### 3. Update Event Agents (`django_ai/automation/agents/core.py`)
- Add `ContextMixin` to agent classes
- Update handler execution to use `set_context()` / `reset_context()`
- Remove `context` parameter from handler signatures

### 4. Update Conversations (`django_ai/conversations/`)
- Add `ContextMixin` to `ConversationAgent`
- Update service to use `set_context()` / `reset_context()`
- Remove separate `_current_agent_context` contextvar

### 5. Update All Existing Code
- Workflows: `ctx = get_context()` → `self.context`
- Event agents: `def handle(self, context, events)` → `def handle(self, events)` + `self.context`
- Conversations: Already uses `self.context` (minimal changes)

## File Changes

| File | Change |
|------|--------|
| `django_ai/context.py` | **New** - contextvar, ContextMixin |
| `django_ai/automation/workflows/core.py` | Use new context API |
| `django_ai/automation/agents/core.py` | Use new context API, remove context param |
| `django_ai/conversations/base.py` | Use ContextMixin |
| `django_ai/conversations/context.py` | Remove `_current_agent_context`, use shared |
| `django_ai/conversations/service.py` | Use new context API |
| All workflow classes | `get_context()` → `self.context` |
| All agent handlers | Remove context param, use `self.context` |

## Testing

1. Workflow step can access `self.context`
2. Agent handler can access `self.context`
3. Conversation agent can access `self.context`
4. Context changes are persisted after execution
5. Nested contexts work (workflow calls subflow)
6. Error in execution still saves context
7. Context not available outside execution raises clear error
