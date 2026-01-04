# Unified Agent Architecture

## Summary

1. **Simplify `django_ai.conversations`** - core data exchange and auto-responding only, remove tool registry/loadout
2. **Add `django_ai.integrations.litellm`** - optional LLM integration with `@tool` decorator and `LiteLLMAgent`
3. **Unify agent abstraction** - one agent can work in conversation mode, event mode, or standalone

## Prerequisites

- **TICKET-unified-context-api.md** - Unified context API (`self.context` everywhere)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  django_ai.conversations (core infrastructure)              │
│                                                             │
│  Data Exchange:                                             │
│  - ConversationSession (context persistence)                │
│  - ConversationMessage (history)                            │
│  - ConversationWidget (interactive widgets)                 │
│  - ResponseStream (Pusher streaming)                        │
│                                                             │
│  Auto-responding:                                           │
│  - ConversationService.send_message() calls get_response()  │
│  - process_conversation_message task                        │
│  - Message processing_status (optimistic UI)                │
│                                                             │
│  No LLM opinion - just calls get_response()                 │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ optional import
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  django_ai.integrations.litellm                             │
│                                                             │
│  - @tool decorator (converts methods to litellm tools)      │
│  - LiteLLMAgent mixin with run_until()                      │
│  - Customizable termination via lambda                      │
│  - Context-aware (self.context persistence)                 │
└─────────────────────────────────────────────────────────────┘
```

## Part 1: Simplify django_ai.conversations

### Keep (preserve all existing behavior)

| Component | Description |
|-----------|-------------|
| `ConversationSession` | Session with context (JSON), user/anonymous_id, status |
| `ConversationMessage` | Messages with type, content, files, processing_status |
| `ConversationWidget` | Widgets with type, data, display_data |
| `ResponseStream` | Pusher streaming (stream_start, text_chunk, stream_end) |
| `ConversationService` | create_session(), send_message() |
| `display_widget()` | Create widget + Pusher event |
| `update_widget()` | Update widget + Pusher event |
| `get_context()` | Context from contextvar |
| `_auto_save_context()` | Save context to session |
| `pusher_auth` view | Channel subscription auth |
| `process_conversation_message` task | Async message processing |
| Anonymous user support | anonymous_id on sessions |
| Files support | ManyToMany on messages |

### Remove

| Component | Reason |
|-----------|--------|
| `django_ai/tools/registry.py` | Tools defined inline with @tool |
| `django_ai/tools/models.py` (ToolLoadout) | No dynamic tool loading |
| `SessionToolLoadout` | No dynamic tool loading |
| `ConversationAgent.Tools.base/available/max_additional` | Use @tool instead |
| `ConversationAgent._validate_tools_config()` | Not needed |
| `ConversationAgent._validate_base_tools()` | Not needed |
| `ConversationAgent.tools` property | Not needed |

### Simplify ConversationAgent

Before:
```python
class ConversationAgent(ABC):
    Context: type[BaseModel] = None

    class Tools:
        base: List[str] = []
        available: List[str] = []
        max_additional: Optional[int] = 5

    def __init__(self):
        # Validates Context, Tools config, base tools in registry
        ...

    @property
    def tools(self) -> List[Callable]:
        # Gets tools from registry
        ...

    @abstractmethod
    def get_response(self, message, request=None, files=None, **kwargs) -> str:
        pass
```

After:
```python
class ConversationAgent:
    """
    Base class for conversation agents.

    Only requirement: implement get_response().
    Context is optional but recommended for state persistence.
    """

    Context: type[BaseModel] = None

    @property
    def context(self):
        """Current agent context (from session contextvar)."""
        from .context import get_context
        return get_context()

    @property
    def session(self):
        """Current conversation session."""
        from .context import _current_session
        return _current_session.get()

    @classmethod
    def create_context(cls, request=None, **kwargs) -> BaseModel:
        """Create initial context. Override if using Context."""
        if cls.Context:
            return cls.Context(**kwargs)
        return None

    def get_response(self, message: str, request=None, files=None, **kwargs) -> str:
        """Generate response. Must be implemented by subclass."""
        raise NotImplementedError
```

### Update Registry

Accept any class with `get_response()`:

```python
def register(self, name: str, agent_class: Type):
    """Register an agent class."""
    if not hasattr(agent_class, 'get_response'):
        raise ValueError(f"Agent '{name}' must have a get_response() method")

    if not callable(getattr(agent_class, 'get_response')):
        raise ValueError(f"Agent '{name}'.get_response must be callable")

    self._agents[name] = agent_class
```

## Part 2: django_ai.integrations.litellm

### File Structure

```
django_ai/integrations/
├── __init__.py
└── litellm.py
```

### @tool Decorator

```python
# django_ai/integrations/litellm.py

def tool(func):
    """
    Mark a method as an LLM tool.

    The method's:
    - Name becomes the tool name
    - Docstring becomes the tool description
    - Type hints become parameter schemas

    Example:
        @tool
        def search(self, query: str, limit: int = 10) -> list:
            '''Search for items matching the query.'''
            ...
    """
    func._is_tool = True
    return func
```

### Turn Object (for run_until lambda)

```python
@dataclass
class Turn:
    """Information about a single LLM turn."""
    iteration: int
    content: str  # LLM text response
    tool_calls: list[dict]  # Tool calls made this turn
    tool_results: list[dict]  # Results from tool execution

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def called(self, tool_name: str) -> bool:
        """Check if a specific tool was called this turn."""
        return any(tc["function"]["name"] == tool_name for tc in self.tool_calls)

    def get_result(self, tool_name: str) -> any:
        """Get result from a specific tool call."""
        for i, tc in enumerate(self.tool_calls):
            if tc["function"]["name"] == tool_name:
                return self.tool_results[i] if i < len(self.tool_results) else None
        return None
```

### LiteLLMAgent Mixin

```python
class LiteLLMAgent:
    """
    Mixin providing LLM + tools functionality via litellm.

    Features:
    - @tool methods auto-collected and converted to litellm format
    - run_until() for LLM execution with customizable termination
    - Context persistence (works with conversations and event agents)
    - ResponseStream integration for real-time streaming

    Example:
        class MyAgent(LiteLLMAgent, ConversationAgent):
            class Context(BaseModel):
                user_name: str = None

            @tool
            def search(self, query: str) -> list:
                '''Search for items.'''
                return db.search(query)

            def system_prompt(self) -> str:
                return f"You are helping {self.context.user_name}"

            def get_response(self, message, **kwargs):
                return self.run_until(prompt=message)
    """

    # Override in subclass
    model: str = "anthropic/claude-sonnet-4-20250514"
    max_iterations: int = 10

    def system_prompt(self) -> str:
        """
        Return the system prompt. Override to customize.

        Can access self.context for dynamic prompts.
        """
        return ""

    @property
    def context(self):
        """
        Current agent context.

        In conversation mode: from ConversationSession.context
        In event mode: from AgentRun.context
        Standalone: from self._context
        """
        # Try conversation context
        try:
            from django_ai.conversations.context import get_context
            ctx = get_context()
            if ctx:
                return ctx
        except:
            pass

        # Fall back to instance context
        return getattr(self, '_context', None)

    @context.setter
    def context(self, value):
        self._context = value

    def _collect_tools(self) -> tuple[list[dict], dict[str, callable]]:
        """
        Collect @tool methods and convert to litellm format.

        Returns:
            (tool_schemas, tool_map)
        """
        from litellm.utils import function_to_dict

        tool_schemas = []
        tool_map = {}

        for attr_name in dir(self):
            if attr_name.startswith('_'):
                continue
            attr = getattr(self, attr_name, None)
            if callable(attr) and getattr(attr, '_is_tool', False):
                schema = function_to_dict(attr)
                tool_schemas.append({"type": "function", "function": schema})
                tool_map[schema["name"]] = attr

        return tool_schemas, tool_map

    def _execute_tool(self, tool_map: dict, tool_call: dict) -> str:
        """Execute a tool call and return result as string."""
        import json

        function_name = tool_call["function"]["name"]
        function_args = tool_call["function"].get("arguments", "{}")

        if isinstance(function_args, str):
            function_args = json.loads(function_args) if function_args else {}

        tool_func = tool_map.get(function_name)
        if not tool_func:
            return f"Error: Tool '{function_name}' not found"

        try:
            result = tool_func(**function_args)
            return str(result) if result is not None else "OK"
        except Exception as e:
            return f"Error: {str(e)}"

    def run_until(
        self,
        prompt: str,
        until: callable = None,
        model: str = None,
        stream: bool = None,
        messages: list = None,
    ) -> str:
        """
        Run LLM with tools until condition is met.

        Args:
            prompt: User prompt
            until: Lambda(turn: Turn) -> bool. Returns True to stop.
                   Default: lambda turn: not turn.has_tool_calls
            model: Override self.model
            stream: Whether to stream. Auto-detects if ResponseStream available.
            messages: Optional message history to continue from

        Returns:
            Final response content

        Example:
            # Stop when no more tool calls (default)
            self.run_until(prompt="Help me")

            # Stop when specific tool called
            self.run_until(prompt="...", until=lambda t: t.called("respond"))

            # Stop after N iterations
            self.run_until(prompt="...", until=lambda t: t.iteration >= 3)

            # Stop on content match
            self.run_until(prompt="...", until=lambda t: "DONE" in t.content)
        """
        import litellm

        # Default termination: no tool calls
        if until is None:
            until = lambda turn: not turn.has_tool_calls

        # Collect tools
        tool_schemas, tool_map = self._collect_tools()

        # Build messages
        if messages is None:
            messages = []
            system = self.system_prompt()
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

        # Auto-detect streaming
        if stream is None:
            try:
                from django_ai.conversations.context import _current_session
                stream = _current_session.get() is not None
            except:
                stream = False

        # Get or create ResponseStream if streaming in conversation context
        response_stream = None
        if stream:
            try:
                from django_ai.conversations.context import ResponseStream, _current_session
                if _current_session.get():
                    response_stream = ResponseStream()
                    response_stream.start()
            except:
                pass

        final_content = ""

        try:
            for iteration in range(self.max_iterations):
                # Call LLM
                response = litellm.completion(
                    model=model or self.model,
                    messages=messages,
                    tools=tool_schemas if tool_schemas else None,
                    stream=stream,
                )

                # Process response
                if stream:
                    content, tool_calls = self._process_stream(response, response_stream)
                else:
                    choice = response.choices[0]
                    content = choice.message.content or ""
                    tool_calls = getattr(choice.message, 'tool_calls', None) or []
                    # Convert to dict format
                    tool_calls = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        for tc in tool_calls
                    ]

                # Execute tool calls
                tool_results = []
                if tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tool_calls
                    })

                    for tool_call in tool_calls:
                        result = self._execute_tool(tool_map, tool_call)
                        tool_results.append(result)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": result
                        })

                # Build turn info
                turn = Turn(
                    iteration=iteration,
                    content=content,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                )

                final_content = content

                # Check termination condition
                if until(turn):
                    break

            return final_content

        finally:
            if response_stream:
                response_stream.end()

    def _process_stream(self, response, response_stream) -> tuple[str, list]:
        """Process streaming response, write to ResponseStream."""
        collected_content = ""
        collected_tool_calls = []

        for chunk in response:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            # Stream content
            if hasattr(delta, 'content') and delta.content:
                collected_content += delta.content
                if response_stream:
                    response_stream.write(delta.content)

            # Collect tool calls
            if hasattr(delta, 'tool_calls') and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    if tc_delta.index is not None:
                        while len(collected_tool_calls) <= tc_delta.index:
                            collected_tool_calls.append({
                                "id": None,
                                "type": "function",
                                "function": {"name": "", "arguments": ""}
                            })

                        tc = collected_tool_calls[tc_delta.index]
                        if tc_delta.id:
                            tc["id"] = tc_delta.id
                        if hasattr(tc_delta, 'function'):
                            if tc_delta.function.name:
                                tc["function"]["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                tc["function"]["arguments"] += tc_delta.function.arguments

        return collected_content, collected_tool_calls
```

## Part 3: Usage Examples

### Conversation Agent with LLM

```python
from django_ai.conversations import ConversationAgent, register_agent
from django_ai.integrations.litellm import LiteLLMAgent, tool
from pydantic import BaseModel

class GuestSupportAgent(LiteLLMAgent, ConversationAgent):
    """Guest support agent with tools."""

    class Context(BaseModel):
        guest_name: str = None
        reservation_id: int = None
        issues_discussed: list[str] = []

    @classmethod
    def create_context(cls, request=None, **kwargs):
        return cls.Context(**kwargs)

    def system_prompt(self) -> str:
        ctx = self.context
        return f"""You are a guest support agent.

Guest: {ctx.guest_name or 'Unknown'}
Reservation: {ctx.reservation_id}
Previous issues: {ctx.issues_discussed}

Help the guest with their questions. Use tools when needed."""

    @tool
    def lookup_reservation(self, reservation_id: int) -> dict:
        """Look up reservation details."""
        from business.pms.models import Reservation
        res = Reservation.objects.get(pk=reservation_id)
        return {"checkin": str(res.checkin_at), "checkout": str(res.checkout_at)}

    @tool
    def submit_maintenance_request(self, description: str, priority: str = "normal") -> str:
        """Submit a maintenance request for the guest's unit."""
        # Create ticket...
        self.context.issues_discussed.append(f"maintenance:{description}")
        return f"Maintenance request submitted: {description}"

    def get_response(self, message, **kwargs):
        return self.run_until(prompt=message)

# Register for HTTP chat
register_agent("guest_support", GuestSupportAgent)
```

### Event Agent with LLM

```python
from django_ai.automation.agents import agent, handler
from django_ai.integrations.litellm import LiteLLMAgent, tool
from pydantic import BaseModel
from datetime import timedelta

@agent("guest_intent", spawn_on="reservation_confirmed", match={"id": "{{ context.reservation_id }}"})
class GuestIntentAgent(LiteLLMAgent):
    """Event-driven agent that uses LLM for message handling."""

    class Context(BaseModel):
        reservation_id: int
        thread_id: int = None

    @classmethod
    def create_context(cls, event):
        return cls.Context(reservation_id=event.entity.pk)

    def system_prompt(self) -> str:
        return """You are handling guest messages.

Classify the message, handle any intents, and respond appropriately."""

    @tool
    def classify(self, message: str) -> list[str]:
        """Classify guest message to detect intents."""
        # Classification logic...
        return ["early_check_in_request"]

    @tool
    def handle_intent(self, intent_type: str) -> dict:
        """Handle an intent by running its workflow."""
        # Start workflow...
        return {"status": "completed", "message": "Early check-in approved"}

    @tool
    def respond(self, message: str):
        """Send a message to the guest."""
        # Send via Channex...
        return "sent"

    @handler("channex_message_received", debounce=timedelta(seconds=5))
    def handle_message(self, ctx, events):
        message = events[-1].entity
        self.run_until(
            prompt=f"Guest message: {message.content}",
            until=lambda turn: turn.called("respond")
        )
```

### Standalone Usage

```python
from django_ai.integrations.litellm import LiteLLMAgent, tool
from pydantic import BaseModel

class AnalysisAgent(LiteLLMAgent):
    """Standalone agent for data analysis."""

    class Context(BaseModel):
        dataset_id: int

    def system_prompt(self):
        return "You are a data analyst. Analyze the dataset and provide insights."

    @tool
    def query_data(self, sql: str) -> list:
        """Run SQL query on the dataset."""
        ...

    @tool
    def create_chart(self, chart_type: str, data: dict) -> str:
        """Create a chart visualization."""
        ...

# Direct usage
agent = AnalysisAgent()
agent.context = AnalysisAgent.Context(dataset_id=123)
result = agent.run_until(
    prompt="What are the top 10 customers by revenue?",
    until=lambda t: t.called("create_chart") or t.iteration >= 5
)
```

## Migration Guide

### From ChatGPTAgent

Before:
```python
class MyAgent(ConversationAgent):
    class Tools:
        base = ["tool1", "tool2"]

    def get_response(self, message, **kwargs):
        # Manual litellm calls, tool execution, streaming...
```

After:
```python
class MyAgent(LiteLLMAgent, ConversationAgent):
    @tool
    def tool1(self, ...):
        ...

    @tool
    def tool2(self, ...):
        ...

    def get_response(self, message, **kwargs):
        return self.run_until(prompt=message)
```

### From External Tools

Before:
```python
# tools/my_tools.py
@register_tool(category="search")
def search_database(query: str) -> list:
    ...

# agent.py
class MyAgent(ConversationAgent):
    class Tools:
        base = ["search_database"]
```

After:
```python
# agent.py
class MyAgent(LiteLLMAgent, ConversationAgent):
    @tool
    def search_database(self, query: str) -> list:
        """Search the database."""
        ...
```

## File Changes Summary

| File | Change |
|------|--------|
| `django_ai/integrations/__init__.py` | New module |
| `django_ai/integrations/litellm.py` | New - @tool, Turn, LiteLLMAgent |
| `django_ai/conversations/base.py` | Simplify - remove Tools validation |
| `django_ai/conversations/registry.py` | Simplify - just check get_response exists |
| `django_ai/conversations/models.py` | Remove SessionToolLoadout |
| `django_ai/tools/` | Delete entire module (or deprecate) |

## Testing

1. **@tool decorator** - methods marked correctly
2. **Tool collection** - _collect_tools finds all @tool methods
3. **Litellm conversion** - schemas are valid
4. **Tool execution** - tools called with correct args, results returned
5. **run_until default** - stops when no tool calls
6. **run_until custom** - lambda conditions work
7. **Streaming** - ResponseStream integration works
8. **Context in conversation** - loaded/saved from session
9. **Context in event agent** - loaded/saved from AgentRun
10. **Context standalone** - manual context works
11. **system_prompt method** - called and can access context
12. **Backward compat** - existing ConversationAgent subclasses still work
