# AI-First Django

> Django patterns and primitives for building autonomous, AI-first business applications

## What This Is

Most AI frameworks focus on synchronous agent interactions. But real-world business applications unfold over days and weeks - a guest books a reservation, gets welcome messages, reminders before arrival, check-in assistance, and follow-ups after departure. 

This repository implements four core primitives that make AI-first business applications maintainable and robust:

1. **Business Event Systems** - Turn model CRUD into business events that stay automatically synced
2. **Workflows & Automations** - Durable multi-step processes combining traditional logic with LLM interactions  
3. **Chat Agents** - Conversational interfaces with rich widgets and persistent context
4. **Business Agents** - Long-running autonomous actors that handle business processes

## Quick Start

```python
# 1. Define business events on your models
class Reservation(models.Model):
    checkin_date = models.DateTimeField()
    status = models.CharField(max_length=20, default="pending")
    
    events = [
        EventDefinition("reservation_confirmed", 
                       condition=lambda r: r.status == "confirmed"),
        EventDefinition("checkin_due", 
                       date_field="checkin_date",
                       condition=lambda r: r.status == "confirmed"),
    ]

# 2. Create simple automations
@on_event("reservation_confirmed")
def send_welcome_email(event):
    reservation = event.entity
    send_email(reservation.guest_email, "Welcome!")

# 3. Build complex workflows  
@event_workflow("checkin_due", offset_minutes=-60)
class SmartLockSetup:
    class Context(BaseModel):
        code: str = ""
    
    @step(start=True)
    def generate_code(self):
        ctx = get_context()
        ctx.code = generate_random_code()
        return goto(self.send_to_lock)
    
    @step(retry=Retry(max_attempts=3))
    def send_to_lock(self):
        # Reliable API calls with automatic retries
        return complete()

# 4. Deploy autonomous business agents
@agent(spawn_on=["guest_checked_in"], heartbeat=timedelta(hours=2))
class GuestConciergeAgent:
    def act(self):
        # Long-running stateful agent that handles guest experience
        pass
```

## Core Features

### 🔄 **Normalized Event System**
- Events stay automatically synced with model changes
- No cascade updates or stale data
- Immediate and scheduled events

### 🔧 **Durable Workflows**  
- Multi-step processes with retries and error handling
- LLM integration with persistent context
- Control flow functions (goto, sleep, wait, complete)

### 💬 **Chat Agents**
- Real-time streaming responses  
- Rich UI widgets alongside text
- Context merging for LLM tool calls

### 🤖 **Business Agents**
- Autonomous actors with internal monologue
- Event-driven and time-based triggering
- Disciplined customer communication

## Installation

```bash
pip install -r requirements.txt
python manage.py migrate
```

See [`requirements.txt`](requirements.txt) for the minimal dependencies (Django, Pydantic, Django-Q2, and a few others).

## Status

🚧 **Code Patterns for Discussion** - This repository contains working reference implementations of the four primitives, extracted from a production AI-first property management application. 

**Current state:**
- ✅ Working code patterns and examples
- ✅ Comprehensive test suite demonstrating usage
- ✅ Detailed documentation with real-world scenarios
- 🔄 APIs still evolving based on community feedback
- ❌ Not yet packaged as an installable Django app

We're sharing these patterns to start a conversation about foundational tools for AI-first business applications. The goal is to refine these abstractions with other developers facing similar challenges.

## Contributing

If you're building AI-first software and want to help develop these foundational patterns:

1. Try the primitives in your application
2. Share feedback on what works and what doesn't  
3. Contribute improvements and new patterns

Let's solve the spaghetti code problem in AI-first applications together.

## Architecture

```
automation/
├── events/          # Business event system  
├── workflows/       # Durable multi-step workflows
├── agents/          # Long-running business agents
└── queues/          # Task orchestration (Django-Q2)

conversations/       # Chat agents with rich UI
tests/              # Comprehensive test suite
```

## License

MIT - See [LICENSE](LICENSE) for details.