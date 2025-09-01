# Automations & Workflows — Dev Guide

> Scope: how to define **events** (immediate & scheduled), write **single-step automations** with `@on_event`, build **event-triggered workflows** (incl. offsets), and use **control flows** (`goto`, `sleep`, `wait`, `wait_for_event`, retries).
> Runtime: execution is auto-scheduled via Django-Q; **no manual polling** in normal operation.

---

## 0) Concepts (10-second read)

* **Immediate event**: `EventDefinition("name", condition=...)` — occurs on commit when condition is true.
* **Scheduled event**: `EventDefinition("name", date_field="...")` — occurs at that datetime (still gated by condition).
* **Single-step automation**: `@on_event("name") def handler(event): ...` — simple “event → action”.
* **Workflow**: `@event_workflow("name", offset_minutes=±N)` + `@step(...)` — multi-step, offsets, waits, retries.
* **Waiting primitives**

  * `wait_for_event("name", ...)` → pauses until an **Event model** named `name` **occurs**. (Integration auto-bridges events to the signal channel `event:<name>`.)
  * `wait("custom:signal", ...)` → pauses until **you** call `engine.signal("custom:signal", payload)`.

And yes: **`event.entity`** resolves the underlying model instance (via ContentType), so you can use that directly.

---

## 1) Declare Events on a Model

```python
# models.py
from django.db import models
from automation.events.definitions import EventDefinition

class Reservation(models.Model):
    checkin_at = models.DateTimeField()
    status = models.CharField(max_length=20, default="pending")

    events = [
        # Immediate: occurs after commit if condition is true
        EventDefinition("reservation_confirmed", condition=lambda r: r.status == "confirmed"),
        # Scheduled: occurs at checkin_at (still gated by condition)
        EventDefinition("checkin_due", date_field="checkin_at", condition=lambda r: r.status == "confirmed"),
    ]
```

What happens automatically:

* `post_save`/`post_delete` signals maintain `Event` rows.
* Valid **immediate** events → **occurred** right after commit.
* **Scheduled** events → occur at/after `at` via the queue’s periodic jobs.

---

## 2) Single-Step Automation (use `@on_event`)

For a one-off action when an event occurs—no workflow needed.

```python
# automations/single_step.py
from automation.events.callbacks import on_event
from automation.events.models import Event

@on_event(event_name="reservation_confirmed")  # entity_type optional; omit unless you want to filter
def send_welcome_email(event: Event) -> None:
    reservation = event.entity  # fetches the Reservation instance
    # do side-effect: email/webhook/etc.
```

(You can also use `@on_event_created` or `@on_event_cancelled` when needed.)

---

## 3) Event-Triggered Workflow (offsets / steps / retries)

Use a workflow when you need multiple steps, waits/sleeps, retries, or to run **before/after** the event time.

```python
# automations/workflows.py
from pydantic import BaseModel
from automation.workflows.core import event_workflow, step, complete

@event_workflow(event_name="checkin_due", offset_minutes=-60)  # run 60m before check-in
class PreArrivalReminder:
    class Context(BaseModel):
        reservation_id: int

    @classmethod
    def create_context(cls, event=None):
        reservation = event.entity
        return cls.Context(reservation_id=reservation.id)

    @step(start=True)
    def run(self):
        # send reminder
        return complete()
```

**Offsets**

* `offset_minutes > 0` → after event time.
* `offset_minutes < 0` → before event time.
* If scheduled for the future, the run is created `WAITING` with `wake_at`.

---

## 4) Control Flows (with the correct waiting semantics)

```python
# automations/advanced.py
from datetime import timedelta
from pydantic import BaseModel
from automation.workflows.core import (
    event_workflow, step, get_context,
    goto, sleep, wait, wait_for_event, complete, fail, Retry
)

@event_workflow(
    event_name="checkout_due",
    offset_minutes=-120,  # 2h before checkout
    default_retry=Retry(max_attempts=3, base_delay=timedelta(seconds=2)),
)
class CheckoutOpsWF:
    class Context(BaseModel):
        reservation_id: int
        docs_ready: bool = False
        notified: bool = False
        attempts: int = 0
        final_state: str | None = None

    @classmethod
    def create_context(cls, event=None):
        return cls.Context(reservation_id=event.entity.id)

    # ── Correct: wait_for_event listens for our Event models (via "event:<name>" bridge)
    @step(start=True)
    def await_docs(self):
        ctx = get_context()
        if ctx.docs_ready:
            return goto(self.notify_housekeeping)
        # Wait until the Event model named "guest_docs_uploaded" OCCURS,
        # or jump to fallback after 90 minutes:
        return wait_for_event("guest_docs_uploaded", timeout=timedelta(minutes=90), on_timeout="fallback_docs")

    @step()
    def fallback_docs(self):
        # proceed with default docs
        return goto(self.notify_housekeeping)

    # ── Example: generic wait for a custom signal YOU emit (not tied to Event models)
    @step()
    def pause_until_ops_window(self):
        return wait.for_signal("ops:window_open")  # elsewhere: engine.signal("ops:window_open")

    @step(retry=Retry(max_attempts=5, base_delay=timedelta(seconds=1)))
    def notify_housekeeping(self):
        ctx = get_context()
        ctx.attempts += 1
        if ctx.attempts < 2:
            raise RuntimeError("transient failure")  # triggers per-step retry
        ctx.notified = True
        return goto(self.finalize)

    @step()
    def finalize(self):
        ctx = get_context()
        if not ctx.notified:
            return fail("Housekeeping was not notified")
        ctx.final_state = "ok"
        return complete(final_state="ok")
```

**Emitting signals**

```python
from automation.workflows.core import engine

# For wait_for_event("guest_docs_uploaded"):
# The integration automatically emits: engine.signal("event:guest_docs_uploaded", payload)
# when the Event model occurs. You normally DON'T need to emit this yourself.

# For generic waits:
engine.signal("ops:window_open")                       # wakes any wait("ops:window_open")
engine.signal("ops:window_open", {"foo": 1})           # payload merges into Context if fields exist
```

**Key distinction**

* `wait_for_event("X")` ⇢ listens to **our Event models** named `X`.
  The integration layer emits `engine.signal("event:X", payload)` on occurrence.
* `wait("something")` ⇢ listens to **your custom signal string**, and only wakes when you call `engine.signal("something", ...)`.

---

## 5) Manual Workflow (optional)

```python
from pydantic import BaseModel
from automation.workflows.core import workflow, step, complete, engine

@workflow("reindex_listing")
class ReindexListing:
    class Context(BaseModel):
        listing_id: int

    @classmethod
    def create_context(cls, listing_id: int):
        return cls.Context(listing_id=listing_id)

    @step(start=True)
    def run(self):
        # do reindex now
        return complete()

# Start manually:
engine.start("reindex_listing", listing_id=123)
```

---

## 6) Queue & Scheduling (already wired)

* `WorkflowsConfig.ready()` sets the executor (Django-Q2) and loads the **event→workflow** bridge:

  * On event occurrence → `engine.handle_event_occurred(event)` starts matching `@event_workflow`s.
  * Also emits `engine.signal("event:<name>", payload)` to wake `wait_for_event("<name>")`.
* `QueuesConfig.ready()` installs schedules (unless `SKIP_Q2_AUTOSCHEDULE=True`):

  * **Workflows: process\_scheduled** (every minute)
  * **Events: poll\_due** (every 5 minutes)
  * **Events: cleanup\_old** (daily)

👉 **No manual calls** in production. Use the task functions only in tests/dev.

---

## 7) Gotchas (as-coded)

* **Immediate events + poller**: the poller considers recent immediate events by creation time window and doesn’t re-check `is_valid`. If you want stricter behavior, tighten `EventProcessor.process_due_events` to require `event.is_valid` for immediate events too.
* **Context merge on signal**: payload keys only merge if they exist on your `Context`.
* **`entity_type`** in decorators is optional (defaults to `"*"`). Omit it unless you need to filter.
* **Use `event.entity`** in callbacks and `create_context`.

---

## Cheatsheet

* Immediate: `EventDefinition("name", condition=...)`
* Scheduled: `EventDefinition("name", date_field="dt", condition=...)`
* Single-step: `@on_event("name") def handler(event): obj = event.entity`
* Workflow (offsets): `@event_workflow("name", offset_minutes=±N)`
* Control flows: `goto`, `sleep`,
  `wait_for_event("EventName", timeout, on_timeout)`,
  `wait("custom:signal", timeout, on_timeout)`,
  `complete()`, `fail("reason")`, `Retry(...)`