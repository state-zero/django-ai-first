# Time Machine - Test Utility for Time-Based Automation

A testing utility that provides controllable time manipulation for testing Django AI First workflows, agents, scheduled events, and time-based operations.

## Overview

The `time_machine` utility allows you to:

- Freeze time at a specific point
- Advance time by arbitrary amounts
- Automatically process due workflows, events, and handlers when time advances
- Run workflows to completion without waiting for real time to pass

## Installation

The utility is included in the `django_ai.automation.testing` module:

```python
from django_ai.automation.testing import time_machine, TimeMachine, with_time_machine
```

## Basic Usage

### Context Manager

```python
from django_ai.automation.testing import time_machine

def test_workflow_with_sleep():
    with time_machine() as tm:
        # Start a workflow that sleeps for 30 minutes
        run = engine.start("my_workflow")
        engine.execute_step(run.id, "start")

        # Workflow is now sleeping
        run.refresh_from_db()
        assert run.status == WorkflowStatus.WAITING

        # Advance time past the sleep duration
        tm.advance(minutes=31)

        # Workflow should have woken and completed
        run.refresh_from_db()
        assert run.status == WorkflowStatus.COMPLETED
```

### Decorator

```python
from django_ai.automation.testing import with_time_machine

class TestWorkflows(TestCase):
    @with_time_machine()
    def test_sleep_workflow(self, tm):
        run = engine.start("sleeper")
        tm.advance(hours=1)
        # ...

    @with_time_machine(start=datetime(2024, 6, 15, 9, 0, tzinfo=timezone.utc))
    def test_at_specific_time(self, tm):
        # Test starts at June 15, 2024 9:00 AM UTC
        # ...
```

## API Reference

### `time_machine()` Context Manager

```python
time_machine(
    start: datetime = None,      # Starting time (defaults to now)
    auto_process: bool = True,   # Auto-process due work on advance
    background_loop: bool = False,  # Run processing in background thread
    tick_interval: float = 0.01  # Background loop interval (seconds)
)
```

### `TimeMachine` Methods

#### Time Control

| Method | Description |
|--------|-------------|
| `advance(seconds=0, minutes=0, hours=0, days=0, delta=None)` | Advance time by the specified amount |
| `advance_to(target: datetime)` | Advance to a specific datetime |
| `freeze(at: datetime)` | Freeze time at a specific point |

#### Processing Control

| Method | Description |
|--------|-------------|
| `process()` | Manually process all due events, workflows, and handlers |
| `run_until_idle(max_iterations=100)` | Keep processing until no more work is due |
| `run_workflow_to_completion(run_id, max_advance, step_size)` | Auto-advance time until workflow completes |

#### Background Processing

| Method | Description |
|--------|-------------|
| `start_background_loop(tick_interval=0.01)` | Start continuous background processing |
| `stop_background_loop()` | Stop the background processing loop |

### Processing Stats

Each processing method returns a `ProcessingStats` object:

```python
stats = tm.advance(hours=1)
print(f"Events processed: {stats.events_processed}")
print(f"Workflows woken: {stats.workflows_woken}")
print(f"Handlers executed: {stats.handlers_executed}")
```

Cumulative stats are also tracked on the `TimeMachine` instance:

```python
print(f"Total events: {tm.stats.events_processed}")
```

## Common Patterns

### Testing Sleep/Wake Cycles

```python
with time_machine() as tm:
    run = engine.start("sleeper_workflow")
    engine.execute_step(run.id, "start")

    # Verify workflow is sleeping
    run.refresh_from_db()
    assert run.status == WorkflowStatus.WAITING
    assert run.wake_at is not None

    # Advance past wake time
    tm.advance(minutes=31)

    # Verify workflow woke and continued
    run.refresh_from_db()
    assert run.status == WorkflowStatus.COMPLETED
```

### Testing Scheduled Events

```python
with time_machine() as tm:
    # Create an event scheduled for 1 hour from now
    obj = ScheduledModel.objects.create(due_at=timezone.now() + timedelta(hours=1))

    # Advance time past the scheduled time
    tm.advance(hours=1, minutes=1)

    # Event should be processed, workflows triggered
    event = Event.objects.get(entity_id=str(obj.pk))
    assert event.status == EventStatus.PROCESSED
```

### Running Workflow to Completion

```python
with time_machine() as tm:
    run = engine.start("multi_step_workflow")
    engine.execute_step(run.id, "start")

    # Auto-advance time until workflow completes
    final_run = tm.run_workflow_to_completion(
        run.id,
        max_advance=timedelta(days=1),  # Give up after 1 day simulated
        step_size=timedelta(minutes=1)   # Advance 1 minute at a time
    )

    assert final_run.status == WorkflowStatus.COMPLETED
```

### Manual Processing Control

```python
with time_machine(auto_process=False) as tm:
    run = engine.start("sleeper")
    engine.execute_step(run.id, "start")

    # Advance time without processing
    tm.advance(hours=1)

    # Workflow still sleeping (not auto-processed)
    run.refresh_from_db()
    assert run.status == WorkflowStatus.WAITING

    # Manually trigger processing
    stats = tm.process()

    # Now workflow should be awake
    run.refresh_from_db()
    assert run.status == WorkflowStatus.COMPLETED
```

## Test Setup

For workflow tests, ensure you're using the synchronous executor:

```python
from django.test import TransactionTestCase, override_settings
from django_ai.automation.workflows.core import engine
from django_ai.automation.queues.sync_executor import SynchronousExecutor

@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class TestMyWorkflows(TransactionTestCase):
    def setUp(self):
        engine.set_executor(SynchronousExecutor())
        # Clear workflow registries if needed
        from django_ai.automation.workflows.core import _workflows
        _workflows.clear()
```

## Testing Agents

The time machine fully supports the agent system, including scheduled handlers with offsets.

### Agent Handler with Offset

```python
@agent("booking_agent", spawn_on="booking_created")
class BookingAgent:
    class Context(BaseModel):
        booking_id: int = 0

    def get_namespace(self, event):
        return f"booking:{event.entity.id}"

    @classmethod
    def create_context(cls, event):
        return cls.Context(booking_id=event.entity.id)

    @handler("move_in", offset=timedelta(minutes=-180))  # 3 hours before checkin
    def send_pre_checkin(self, ctx):
        # Send pre-checkin email
        pass

    @handler("move_in")  # At checkin time
    def send_welcome(self, ctx):
        # Send welcome message
        pass

    @handler("move_in", offset=timedelta(minutes=60))  # 1 hour after checkin
    def send_followup(self, ctx):
        # Send followup
        pass
```

```python
with time_machine() as tm:
    # Checkin is 5 hours from now
    checkin = timezone.now() + timedelta(hours=5)

    booking = Booking.objects.create(
        guest=guest,
        checkin_date=checkin,
        checkout_date=checkin + timedelta(days=2),
        status="confirmed",
    )

    # Agent spawned automatically via booking_created event

    # Advance to 3 hours before checkin (2 hours from now)
    tm.advance(hours=2, minutes=1)
    # pre_checkin handler fired

    # Advance to checkin time
    tm.advance(hours=3)
    # welcome handler fired

    # Advance 1 hour past checkin
    tm.advance(hours=1)
    # followup handler fired
```

### Complete Journey Test

```python
with time_machine() as tm:
    booking = Booking.objects.create(...)

    # Fast-forward through the entire reservation
    tm.advance(days=3)

    # Verify all handlers executed
    agent_run = AgentRun.objects.get(namespace=f"booking:{booking.id}")
    assert agent_run.context["pre_checkin_sent"]
    assert agent_run.context["welcome_sent"]
    assert agent_run.context["checkout_sent"]
```

## Test Setup for Agents

```python
from django.test import TransactionTestCase, override_settings
from django_ai.automation.workflows.core import engine
from django_ai.automation.agents.core import agent_engine, _agents, _agent_handlers
from django_ai.automation.queues.sync_executor import SynchronousExecutor
from django_ai.automation.events.callbacks import callback_registry

@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class TestMyAgents(TransactionTestCase):
    def setUp(self):
        engine.set_executor(SynchronousExecutor())
        agent_engine.set_executor(SynchronousExecutor())

        # Clear registries
        _agents.clear()
        _agent_handlers.clear()
        callback_registry.clear()

        # Register workflow integration (needed for event->workflow triggers)
        from django_ai.automation.workflows.integration import handle_event_for_workflows
        callback_registry.register(
            handle_event_for_workflows, event_name="*", namespace="*"
        )
```

## What Gets Processed

When you call `tm.advance()` or `tm.process()`, the following are processed:

| System | What's Processed |
|--------|------------------|
| Events | Due events (scheduled time has passed) |
| Workflows | Sleeping workflows past their `wake_at` time |
| Agents | Scheduled handler executions past their `scheduled_at` time |

## Notes

- Time only moves forward - attempting to advance backwards raises `ValueError`
- The time machine patches `django.utils.timezone.now()`
- Real time is restored when exiting the context manager
- Use `TransactionTestCase` for tests that need database commits to trigger signals
