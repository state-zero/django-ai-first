from datetime import timedelta
from typing import Optional

from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from pydantic import BaseModel

from tests.models import ITImmediate, ITScheduled
from ..events.models import Event, EventStatus
from ..workflows.core import (
    engine,
    workflow,
    event_workflow,
    step,
    get_context,
    complete,
    wait_for_event,
    sleep,
    goto,
)
from ..workflows.models import WorkflowRun, WorkflowStatus
from ..queues.sync_executor import SynchronousExecutor
from ..testing import time_machine


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class TestEndToEndIntegration(TransactionTestCase):
    def setUp(self):
        engine.set_executor(SynchronousExecutor())
        # reset workflow registries
        from ..workflows.core import _workflows, _event_workflows
        from ..events.callbacks import callback_registry

        _workflows.clear()
        _event_workflows.clear()
        callback_registry.clear()

        from ..workflows.integration import handle_event_for_workflows

        callback_registry.register(
            handle_event_for_workflows, event_name="*", namespace="*"
        )

        # --- event workflow for immediate
        @event_workflow(event_name="immediate_evt")
        class ImmediateWF:
            class Context(BaseModel):
                started: bool = False

            @classmethod
            def create_context(cls, event=None):
                return cls.Context(started=True)

            @step(start=True)
            def start(self):
                return complete()

        # --- event workflow for scheduled
        @event_workflow(event_name="scheduled_evt")
        class ScheduledWF:
            class Context(BaseModel):
                started: bool = False

            @classmethod
            def create_context(cls, event=None):
                return cls.Context(started=True)

            @step(start=True)
            def start(self):
                return complete()

        # --- waiter that listens via engine.signal("event:immediate_evt", ...)
        @workflow("waiter")
        class WaiterWF:
            class Context(BaseModel):
                got_event: bool = False
                event_id: Optional[int] = None  # payload merged by engine.signal(...)

            @classmethod
            def create_context(cls):
                return cls.Context()

            @step(start=True)
            def start(self):
                # Transition to the dedicated waiting step
                return goto(self.wait_for_the_event)

            @wait_for_event("immediate_evt")
            @step()
            def wait_for_the_event(self):
                # This step only executes after the event is received.
                return goto(self.after)

            @step()
            def after(self):
                ctx = get_context()
                ctx.got_event = True
                return complete()

        # --- sleeper to test process_scheduled wakeups
        @workflow("sleeper")
        class SleeperWF:
            class Context(BaseModel):
                woke: bool = False
                slept_once: bool = False

            @classmethod
            def create_context(cls):
                return cls.Context()

            @step(start=True)
            def start(self):
                ctx = get_context()
                if not ctx.slept_once:
                    ctx.slept_once = True
                    return sleep(timedelta(minutes=5))
                return goto(self.after)

            @step()
            def after(self):
                ctx = get_context()
                ctx.woke = True
                return complete()

    def test_immediate_event_triggers_callbacks_workflows_and_signals_waiters(self):
        """Immediate event fires on save, triggers workflow, and signals waiters."""
        with time_machine() as tm:
            waiter_run = engine.start("waiter")
            obj = ITImmediate.objects.create(flag=True)  # fires on_commit -> processed

            ct = ContentType.objects.get_for_model(ITImmediate)
            ev = Event.objects.get(
                model_type=ct, entity_id=str(obj.pk), event_name="immediate_evt"
            )
            self.assertEqual(ev.status, EventStatus.PROCESSED)

            run = WorkflowRun.objects.filter(
                triggered_by_event_id=ev.id, name__startswith="event:immediate_evt"
            ).first()
            self.assertIsNotNone(run)
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)

            # waiter should have advanced automatically on signal()
            waiter_run.refresh_from_db()
            self.assertEqual(waiter_run.status, WorkflowStatus.COMPLETED)
            self.assertTrue(waiter_run.data.get("got_event"))

    def test_scheduled_event_polled_and_triggers_event_workflow(self):
        """Scheduled event in the past is processed when time machine processes."""
        with time_machine() as tm:
            # Create event 1 hour in the future
            future = timezone.now() + timedelta(hours=1)
            obj = ITScheduled.objects.create(due_at=future)

            ct = ContentType.objects.get_for_model(ITScheduled)
            ev = Event.objects.get(
                model_type=ct, entity_id=str(obj.pk), event_name="scheduled_evt"
            )
            self.assertEqual(ev.status, EventStatus.PENDING)

            # Advance past the scheduled time
            tm.advance(hours=1, minutes=1)

            ev.refresh_from_db()
            self.assertEqual(ev.status, EventStatus.PROCESSED)

            run = WorkflowRun.objects.filter(
                triggered_by_event_id=ev.id, name__startswith="event:scheduled_evt"
            ).first()
            self.assertIsNotNone(run)
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)

    def test_sleep_then_wake_via_time_advance(self):
        """Sleeping workflow wakes up when time advances past wake_at."""
        with time_machine() as tm:
            run = engine.start("sleeper")
            engine.execute_step(run.id, "start")

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.WAITING)
            self.assertIsNotNone(run.wake_at)

            # Advance past the 5-minute sleep
            tm.advance(minutes=6)

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)
            self.assertTrue(run.data.get("woke"))

    def test_immediate_event_with_false_condition_does_not_fire(self):
        """Immediate event with failing condition stays pending."""
        with time_machine() as tm:
            obj = ITImmediate.objects.create(flag=False)
            ct = ContentType.objects.get_for_model(ITImmediate)
            ev = Event.objects.get(
                model_type=ct, entity_id=str(obj.pk), event_name="immediate_evt"
            )

            # Event row exists but should not be processed
            self.assertEqual(ev.status, EventStatus.PENDING)

    def test_scheduled_event_in_future_not_due_yet(self):
        """Scheduled event in the future is not processed until time advances."""
        with time_machine() as tm:
            future = timezone.now() + timedelta(hours=1)
            obj = ITScheduled.objects.create(due_at=future)

            ct = ContentType.objects.get_for_model(ITScheduled)
            ev = Event.objects.get(model_type=ct, entity_id=str(obj.pk))
            self.assertEqual(ev.status, EventStatus.PENDING)

            # Advance only 30 minutes - not enough
            tm.advance(minutes=30)

            ev.refresh_from_db()
            self.assertEqual(ev.status, EventStatus.PENDING)

            # Now advance past the due time
            tm.advance(minutes=31)

            ev.refresh_from_db()
            self.assertEqual(ev.status, EventStatus.PROCESSED)

    def test_scheduled_event_exact_now_triggers(self):
        """Scheduled event at exactly now is processed."""
        with time_machine() as tm:
            now = timezone.now()
            obj = ITScheduled.objects.create(due_at=now)

            ct = ContentType.objects.get_for_model(ITScheduled)
            ev = Event.objects.get(model_type=ct, entity_id=str(obj.pk))
            self.assertEqual(ev.status, EventStatus.PENDING)

            # Process without advancing - event is already due
            tm.process()

            ev.refresh_from_db()
            self.assertEqual(ev.status, EventStatus.PROCESSED)

    def test_waiter_does_not_advance_without_signal(self):
        """Waiter workflow stays suspended until signal arrives."""
        with time_machine() as tm:
            run = engine.start("waiter")
            run.refresh_from_db()
            # Should be stuck in SUSPENDED until a signal arrives
            self.assertEqual(run.status, WorkflowStatus.SUSPENDED)
            self.assertFalse(run.data.get("got_event"))

            # Advancing time doesn't help - it needs a signal
            tm.advance(hours=24)

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.SUSPENDED)

    def test_sleep_workflow_not_prematurely_completed(self):
        """Sleeping workflow stays waiting if time hasn't advanced enough."""
        with time_machine() as tm:
            run = engine.start("sleeper")
            engine.execute_step(run.id, "start")

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.WAITING)

            # Advance only 2 minutes - sleep is 5 minutes
            tm.advance(minutes=2)

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.WAITING)

    def test_multiple_due_scheduled_events_processed(self):
        """Multiple scheduled events are all processed when time advances."""
        with time_machine() as tm:
            future = timezone.now() + timedelta(hours=1)
            objs = [ITScheduled.objects.create(due_at=future) for _ in range(3)]

            # Advance past all of them
            tm.advance(hours=1, minutes=1)

            for obj in objs:
                ev = Event.objects.get(entity_id=str(obj.pk))
                self.assertEqual(ev.status, EventStatus.PROCESSED)

    def test_immediate_event_invalid_is_not_processed_by_poller(self):
        """Invalid immediate event is not processed even when polling."""
        with time_machine() as tm:
            obj = ITImmediate.objects.create(flag=False)

            ct = ContentType.objects.get_for_model(ITImmediate)
            ev = Event.objects.get(
                model_type=ct, entity_id=str(obj.pk), event_name="immediate_evt"
            )

            # Sanity: it's pending and invalid
            self.assertEqual(ev.status, EventStatus.PENDING)
            self.assertFalse(ev.is_valid)

            # Processing doesn't change it
            tm.process()

            ev.refresh_from_db()
            self.assertEqual(ev.status, EventStatus.PENDING)
