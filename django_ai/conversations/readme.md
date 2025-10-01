# Django AI Conversations

**Build conversational AI interfaces with real-time streaming, context management, and rich UI widgets**

Django AI Conversations provides the infrastructure for building chat agents in Django applications. Handle user messages, stream responses in real-time, process files, and display interactive widgets - all with a clean, Django-native API.

---

## Why Django AI Conversations?

**The Challenge:** Building conversational interfaces means managing WebSocket connections, streaming responses, maintaining conversation state, handling file uploads, and coordinating with your backend logic.

**The Solution:** Django AI Conversations provides the framework - you just implement the conversation logic in your agent classes.

---

## Quick Start

### 1. Create a Chat Agent

```python
# your_app/chat_agents.py
from django_ai.conversations import ConversationAgent
from django_ai.conversations.context import ResponseStream
from pydantic import BaseModel

class SupportAgent(ConversationAgent):
    """Customer support agent"""
    
    class Context(BaseModel):
        user_id: int = 0
        support_tier: str = "basic"
    
    @classmethod
    def create_context(cls, request=None, **kwargs):
        """Build context when session starts"""
        user = request.user if request and request.user.is_authenticated else None
        return cls.Context(
            user_id=user.id if user else 0,
            support_tier=kwargs.get('tier', 'basic')
        )
    
    async def get_response(self, message, request=None, **kwargs):
        """Process user message and return response"""
        
        # Simple response (no streaming)
        if "hello" in message.lower():
            return "Hello! How can I help you today?"
        
        # Streaming response (real-time chunks via WebSocket)
        with ResponseStream() as stream:
            stream.write("Let me check that for you")
            # ... process request ...
            stream.write("Here's what I found...")
        
        return stream.content
```

### 2. Register Your Agent

```python
# your_app/agents.py
from django_ai.conversations.registry import register_agent
from .chat_agents import SupportAgent

register_agent("support", SupportAgent)
register_agent("sales", SalesAgent)
```

### 3. Configure Settings

```python
# settings.py
INSTALLED_APPS = [
    # ... other apps
    'django_ai.conversations',
]

# Optional: Enable real-time streaming
DJANGO_AI_PUSHER = {
    'APP_ID': 'your_pusher_app_id',
    'KEY': 'your_pusher_key',
    'SECRET': 'your_pusher_secret',
    'CLUSTER': 'us2'
}
```

### 4. Use from Your Application

```python
from django_ai.conversations.actions import start_conversation
from django_ai.conversations.service import ConversationService

# Create conversation session
session = start_conversation(
    agent_path="support",
    context_kwargs={"tier": "premium"},
    request=request
)

# Send message
result = ConversationService.send_message(
    session_id=session['session_id'],
    message="I need help with billing",
    user=request.user,
    request=request
)

# Returns: {"status": "success", "response": "...", "session_id": "..."}
```

---

## Core Features

### 🌊 Real-Time Streaming

Stream responses for better UX - chunks are automatically sent via WebSocket:

```python
async def get_response(self, message, request=None, **kwargs):
    with ResponseStream() as stream:
        stream.write("Processing your request")
        
        # Call your AI/LLM here
        result = await your_ai_function(message)
        
        # Stream response word by word
        for word in result.split():
            stream.write(word + " ")
    
    return stream.content  # Full accumulated response
```

Frontend receives `text_chunk` events via Pusher WebSocket automatically.

### 🎯 Context Management

Define strongly-typed context with Pydantic that flows through your agent:

```python
class MyAgent(ConversationAgent):
    class Context(BaseModel):
        user_id: int
        subscription_tier: str
        conversation_count: int = 0
    
    @classmethod
    def create_context(cls, request=None, **kwargs):
        """Called once per session"""
        return cls.Context(
            user_id=request.user.id,
            subscription_tier=kwargs.get('tier', 'free'),
            conversation_count=0
        )
    
    async def get_response(self, message, request=None, **kwargs):
        # Access context
        ctx = self.context
        ctx.conversation_count += 1
        
        return f"Message #{ctx.conversation_count} for user {ctx.user_id}"
```

### 🔧 Context Injection

Automatically inject context into tool functions (perfect for LLM function calling):

```python
from django_ai.conversations.decorators import with_context

class SalesAgent(ConversationAgent):
    class Context(BaseModel):
        user_id: int
        account_tier: str
    
    @with_context()
    def check_eligibility(self, product_id: str, user_id: int, account_tier: str):
        """Context parameters auto-injected - LLM only needs to provide product_id"""
        if account_tier == "premium":
            return f"User {user_id} can access {product_id}"
        return "Upgrade required"
    
    async def get_response(self, message, request=None, **kwargs):
        # Use with LiteLLM, OpenAI, etc. - they only see product_id parameter
        result = self.check_eligibility(product_id="pro-features")
        return result
```

The `@with_context()` decorator preserves function signatures for LLM introspection while injecting context at runtime.

### 🎨 Rich UI Widgets

Display interactive components in the chat:

```python
from django_ai.conversations.context import display_widget

async def get_response(self, message, request=None, **kwargs):
    if "book appointment" in message.lower():
        display_widget("appointment_form", {
            "available_slots": ["2024-01-15 10:00", "2024-01-15 14:00"],
            "callback_url": "/api/book-appointment"
        })
        
        return "Please select your preferred time above."
```

### 📄 File Processing

Handle file uploads with automatic text extraction (PDFs, images, documents):

```python
from django_ai.conversations.context import get_file_text

async def get_response(self, message, request=None, **kwargs):
    if hasattr(self, 'files') and self.files:
        results = []
        
        for file in self.files:
            # Automatic extraction using Apache Tika + OCR
            text = get_file_text(file.id)
            
            if text:
                results.append(f"From {file.filename}: {text[:200]}...")
        
        return "File analysis:\n" + "\n".join(results)
    
    return "Upload a file and I'll analyze it."
```

---

## Architecture

### How It Works

1. **Agent Registration** - Agents are registered with paths like "support", "sales/premium"
2. **Session Creation** - Each conversation gets a session with agent path and context
3. **Message Flow** - User messages → `ConversationService.send_message()` → Agent's `get_response()`
4. **Response Types**:
   - **Non-streaming**: Return string directly (no WebSocket events)
   - **Streaming**: Use `ResponseStream` (sends `text_chunk` events via Pusher)
5. **Storage** - All messages stored in `ConversationMessage` model

### Two Parallel Systems

**Database (via StateZero or Django ORM)**
- Stores conversation sessions and messages
- Provides message history
- Handles CRUD operations

**WebSocket (via Pusher)**
- Streams response chunks in real-time
- Sends widget display events
- Provides live updates during agent processing

They work together but serve different purposes.

---

## API Reference

### ConversationAgent Base Class

```python
class YourAgent(ConversationAgent):
    class Context(BaseModel):
        """Define your context structure"""
        pass
    
    @classmethod
    def create_context(cls, request=None, **kwargs):
        """Create context when session starts"""
        return cls.Context(...)
    
    async def get_response(self, message, request=None, **kwargs):
        """Main handler - return string response"""
        return "response"
```

### ConversationService

```python
from django_ai.conversations.service import ConversationService

# Create session
session = ConversationService.create_session(
    agent_path="support",
    user=request.user,
    anonymous_id="guest_123",  # For anonymous users
    context={"custom": "data"}
)

# Send message
result = ConversationService.send_message(
    session_id=session.id,
    message="user message",
    user=request.user,
    request=request
)
```

### Actions (Recommended)

```python
from django_ai.conversations.actions import start_conversation

# Higher-level API
session = start_conversation(
    agent_path="support",
    context_kwargs={"tier": "premium"},
    request=request
)
```

### Context Utilities

```python
from django_ai.conversations.context import (
    ResponseStream,  # Stream response chunks
    display_widget,  # Show UI component
    get_file_text    # Extract text from files
)

# Streaming
with ResponseStream() as stream:
    stream.write("chunk")
    stream.write("another chunk")
# stream.content has full text

# Widgets
display_widget("widget_type", {"data": "here"})

# Files
text = get_file_text(file_id)
```

---

## Configuration

### Required Settings

```python
INSTALLED_APPS = [
    'django_ai.conversations',
]
```

### Optional Settings

```python
# Enable real-time streaming (requires Pusher account)
DJANGO_AI_PUSHER = {
    'APP_ID': 'your_app_id',
    'KEY': 'your_key',
    'SECRET': 'your_secret',
    'CLUSTER': 'us2'
}

# Custom text extraction
DJANGO_AI = {
    'TEXT_EXTRACTOR': 'your_app.extractors.CustomExtractor'
}
```

---

## Models

```python
from django_ai.conversations.models import (
    ConversationSession,  # Conversation with an agent
    ConversationMessage,  # Individual message
    File                  # Uploaded file
)

# Sessions track conversations
session = ConversationSession.objects.create(
    agent_path="support",
    user=user,  # or None
    context={"initial": "data"},
    status="active"  # active/completed/archived
)

# Messages are automatically created
message = ConversationMessage.objects.filter(
    session=session,
    message_type="agent"  # user/agent/system/widget
)
```

---

## Examples

### Basic Echo Agent

```python
class EchoAgent(ConversationAgent):
    async def get_response(self, message, request=None, **kwargs):
        return f"You said: {message}"
```

### LLM Integration (LiteLLM)

```python
import litellm

class AIAgent(ConversationAgent):
    async def get_response(self, message, request=None, **kwargs):
        with ResponseStream() as stream:
            response = litellm.completion(
                model="gpt-4",
                messages=[{"role": "user", "content": message}],
                stream=True
            )
            
            for chunk in response:
                content = chunk.choices[0].delta.content or ""
                stream.write(content)
        
        return stream.content
```

### Function Calling with Context

```python
class SmartAgent(ConversationAgent):
    class Context(BaseModel):
        user_id: int
        org_id: int
    
    @with_context()
    def get_user_data(self, user_id: int, org_id: int):
        """Context injected automatically"""
        return fetch_user_data(user_id, org_id)
    
    async def get_response(self, message, request=None, **kwargs):
        # Use with LLM function calling
        tools = [self.get_user_data]
        response = litellm.completion(
            model="gpt-4",
            messages=[{"role": "user", "content": message}],
            tools=tools
        )
        # Context injected when LLM calls the function
        return response.choices[0].message.content
```

---

## Requirements

- Django 4.0+
- Python 3.12+
- Pusher account (optional, for real-time features)

## Installation

```bash
pip install django-ai-first
```

## License

Free commercial license - use in personal and commercial projects at no cost.

---

**That's it!** Django AI Conversations provides the infrastructure - you implement the conversation logic.