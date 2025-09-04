# Chat Agents Developer Guide

> Build conversational AI interfaces with real-time streaming, file processing, and rich UI widgets.

Chat agents are **request/response handlers** that power interactive conversations with users. Unlike automation agents (which handle background events), these respond to chat messages.

---

## Quick Start

### 1. Create a Chat Agent

```python
# your_app/chat_agents.py
from pydantic import BaseModel
from conversations.context import ResponseStream, display_widget, get_file_text

class SupportAgent:
    class Context(BaseModel):
        user_id: int = 0
        user_email: str = ""
        conversation_stage: str = "greeting"
    
    def create_context(self):
        """Called when session starts - build your context"""
        return self.Context(
            user_id=self.user.id if self.user else 0,
            user_email=self.user.email if self.user else ""
        )
    
    async def get_response(self, message: str, session_context: dict, **kwargs):
        """Main handler - process user message and return response"""
        
        # Simple response
        if "hello" in message.lower():
            return "Hello! How can I help you today?"
        
        return "I'm here to help with your support needs."
```

### 2. Register Your Agent

```python
# your_app/chat_urls.py
from django.urls import path
from .chat_agents import SupportAgent

chat_urlpatterns = [
    path('support/', SupportAgent.as_view()),
    path('sales/', SalesAgent.as_view()),
]
```

### 3. Configure Settings

```python
# settings.py
DJANGO_AI_CHAT_URLS = "your_app.chat_urls"

# Optional: Real-time features
DJANGO_AI_PUSHER = {
    'KEY': 'your_pusher_key',
    'SECRET': 'your_pusher_secret',
    'APP_ID': 'your_app_id',
    'CLUSTER': 'us2'
}
```

### 4. Use the API

```python
from conversations.service import ConversationService

# Create session
session = ConversationService.create_session(
    agent_path="support/",
    user=request.user
)

# Send message
result = ConversationService.send_message(
    session_id=session.id,
    message="I need help",
    user=request.user
)
```

---

## Core Features

### Real-Time Streaming

Stream responses for better UX:

```python
async def get_response(self, message, session_context, **kwargs):
    with ResponseStream() as stream:
        stream.write("Let me think...")
        
        # Your AI processing here
        response = await call_your_ai(message)
        
        # Stream the response chunk by chunk
        for chunk in response.split():
            stream.write(chunk + " ")
    
    return stream.content  # Full response
```

### Rich UI Widgets

Display interactive components:

```python
async def get_response(self, message, session_context, **kwargs):
    if "book appointment" in message.lower():
        display_widget("appointment_form", {
            "available_slots": ["2024-01-15 10:00", "2024-01-15 14:00"],
            "callback_url": "/api/book-appointment"
        })
        
        return "Please select your preferred appointment time."
```

### File Processing

Handle file uploads with automatic text extraction:

```python
async def get_response(self, message, session_context, **kwargs):
    # Access uploaded files (if any)
    if hasattr(self, 'files') and self.files:
        file_responses = []
        
        for file in self.files:
            # Extract text from PDFs, images, docs (uses Apache Tika + OCR)
            text = get_file_text(file.id)
            
            if text:
                file_responses.append(f"From {file.filename}: {text[:200]}...")
        
        return "I've analyzed your files:\n" + "\n".join(file_responses)
    
    return "Upload a document and I'll analyze it for you."
```

### Context Injection

Automatically inject agent context into functions:

```python
from conversations.decorators import with_context

class SalesAgent:
    class Context(BaseModel):
        user_id: int
        subscription_tier: str = "free"
    
    @with_context()
    def check_user_eligibility(self, product_id: str, user_id: int, subscription_tier: str):
        """Parameters automatically injected from agent context"""
        if subscription_tier == "premium":
            return f"User {user_id} can access {product_id}"
        return "Upgrade required"
    
    async def get_response(self, message, session_context, **kwargs):
        if "check eligibility" in message:
            result = self.check_user_eligibility(product_id="pro-features")
            return result
```

---

## Agent Types

### Class-Based (Recommended)

```python
class MyAgent:
    class Context(BaseModel):
        # Define your context structure
        field1: str = ""
        field2: int = 0
    
    def create_context(self):
        return self.Context(...)
    
    async def get_response(self, message, session_context, **kwargs):
        return "Response"
```

### Function-Based

```python
async def my_agent(message: str, session_context: dict, **kwargs):
    return "Simple response"

# In chat_urls.py
path('simple/', my_agent),
```

---

## Sessions & Messages

### Session Management

```python
# Sessions store conversation state
session = ConversationSession.objects.create(
    agent_path="support/",
    user=user,  # or None for anonymous
    anonymous_id="guest_123",  # for anonymous users
    context={"custom": "data"},  # Initial context
    status="active"  # active/completed/archived
)
```

### Message Types

```python
# Messages are automatically stored
ConversationMessage.objects.create(
    session=session,
    message_type="user",     # user/agent/system/widget
    content="Hello",
    component_type="",       # For widgets
    component_data={},       # Widget data
    files=file_objects       # Attached files
)
```

---

## Configuration

### Text Extraction

```python
# settings.py - Configure text extraction strategy
SAAS_AI = {
    'TEXT_EXTRACTOR': 'your_app.extractors.CustomExtractor'
}

# Custom extractor
from conversations.text_extractor import TextExtractor

class CustomExtractor(TextExtractor):
    def extract_text(self, file_path: str, lang: str = "eng") -> str:
        # Your implementation
        return extracted_text
```

### Real-Time Events

When Pusher is configured, these events are sent automatically:
- `stream_start` - Response streaming begins
- `text_chunk` - Each chunk of streamed text  
- `stream_end` - Response streaming complete
- `widget` - Widget display

---

## Best Practices

### Error Handling

```python
async def get_response(self, message, session_context, **kwargs):
    try:
        return await your_processing(message)
    except Exception as e:
        logger.exception(f"Agent error: {e}")
        return "Sorry, I encountered an error. Please try again."
```

### Context Updates

```python
def create_context(self):
    # Context is created once per session
    # Use session_context dict for per-message data
    return self.Context(persistent_data="here")

async def get_response(self, message, session_context, **kwargs):
    # session_context contains:
    # - user_id, session_id, anonymous_id
    # - Any custom context from session creation
    current_user = session_context.get('user_id')
```

### Performance

```python
class OptimizedAgent:
    def __init__(self):
        # Cache expensive resources at class level
        self.cached_data = load_expensive_data()
    
    async def get_response(self, message, session_context, **kwargs):
        # Use cached resources
        return self.process_with_cache(message)
```

---

## API Reference

### ConversationService

```python
# Create session
session = ConversationService.create_session(
    agent_path: str,           # e.g. "support/"
    user: User = None,         # Django user or None
    anonymous_id: str = None,  # For anonymous users
    context: dict = None       # Initial context data
)

# Send message
result = ConversationService.send_message(
    session_id: UUID,
    message: str,
    user: User = None
)
# Returns: {"status": "success", "response": "...", "session_id": "..."}
```

### Context Utilities

```python
# In agent methods:
display_widget(widget_type: str, data: dict)  # Show UI component
get_file_text(file_id: UUID) -> str           # Extract file text

# ResponseStream
with ResponseStream() as stream:
    stream.start(metadata=dict)    # Optional start metadata
    stream.write(chunk: str)       # Write text chunk
    stream.end()                   # Finish stream
# stream.content contains full accumulated text
```

---

That's it! The conversations app provides the framework - you implement the actual conversational AI logic in your chat agent classes.