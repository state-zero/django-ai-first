# Timers (Internal)

Internal cache-driven scheduling utility for high-precision delayed execution and debouncing.

## Purpose

This module provides scheduling for:

1. **Workflow `sleep()` steps** - Precise wake-up for sleeping workflows
2. **Event callback debouncing** - Batch rapid-fire events before triggering handlers
3. **Queue delayed execution** - When tasks are queued with a `delay` parameter

## How Workflow Sleep Uses Timers

When a workflow step returns `sleep()`:

1. Sets `wake_at` on the WorkflowRun (fallback for if timer fails)
2. Schedules a timer for precise wake-up

This is a **belt-and-suspenders** approach:
- **Timer** provides precision (polls every 100ms)
- **`wake_at`** is the fallback (polled every 1 minute by `process_scheduled`)

```python
# In workflows/core.py Sleep handling
run.wake_at = timezone.now() + result.duration
run.save()

schedule_task(
    "django_ai.automation.queues.tasks.wake_workflow",
    args=[run.id, run.wake_at.isoformat()],  # includes expected time to detect stale timers
    delay=result.duration,
)
```

## Other Internal Usage

### Event Callback Debouncing

When event callbacks are registered with debouncing, rapid events are batched:

```python
# In events/callbacks.py
debounce_task(
    task_name="...dispatch_debounced_callback",
    debounce_key=f"callback:{reg._id}:{debounce_key}",
    debounce_window=reg.debounce_window,
    collect=event.id,
)
```

### Queue Delayed Execution

When a task is queued with a delay:

```python
# In queues/q2_executor.py
if delay:
    from django_ai.automation.timers.core import schedule_task
    return schedule_task(fn, args=list(args), delay=delay)
```

## Architecture

Cache-driven to avoid database locking:

- **Pending tasks** → Cache (fast polling, no DB locks)
- **Executed tasks** → DB (audit log only)
- **Sync thread** → Polls cache every 100ms in dev mode

## Configuration

```python
DJANGO_AI = {
    'TIMERS_IN_PROCESS': True,  # Dev: background thread
                                 # Prod: requires run_timers worker
}
```

## Testing

Use `time_machine` to test without real delays:

```python
from django_ai.automation.testing import time_machine

with time_machine() as tm:
    workflow.start()
    tm.advance(hours=24)
    assert workflow.completed
```
