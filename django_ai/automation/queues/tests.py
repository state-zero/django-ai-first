from datetime import timedelta
from typing import Optional

from django.test import TransactionTestCase, override_settings
from django.db import connection
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
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
from ..queues import tasks as q2_tasks  # the same entrypoints schedules call


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
        callback_registry.register(handle_event_for_workflows, event_name="*", namespace="*")

        from pydantic import BaseModel

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
                ctx = get_context()
                # First entry -> park. Re-entry after signal (event_id present) -> advance.
                if ctx.event_id is None:
                    return wait_for_event("immediate_evt")
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
                    return sleep(timedelta(milliseconds=1))
                return goto(self.after)

            @step()
            def after(self):
                ctx = get_context()
                ctx.woke = True
                return complete()

    def test_immediate_event_triggers_callbacks_workflows_and_signals_waiters(self):
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
        past = timezone.now() - timedelta(minutes=1)
        obj = ITScheduled.objects.create(due_at=past)

        ct = ContentType.objects.get_for_model(ITScheduled)
        ev = Event.objects.get(
            model_type=ct, entity_id=str(obj.pk), event_name="scheduled_evt"
        )
        self.assertEqual(ev.status, EventStatus.PENDING)

        stats = q2_tasks.poll_due_events(hours=24)
        self.assertGreaterEqual(stats["events_processed"], 1)

        ev.refresh_from_db()
        self.assertEqual(ev.status, EventStatus.PROCESSED)
        run = WorkflowRun.objects.filter(
            triggered_by_event_id=ev.id, name__startswith="event:scheduled_evt"
        ).first()
        self.assertIsNotNone(run)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)

    def test_sleep_then_wake_via_process_scheduled(self):
        run = engine.start("sleeper")
        engine.execute_step(run.id, "start")
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.WAITING)
        self.assertIsNotNone(run.wake_at)

        # make it due and run the same function as the Q2 schedule
        WorkflowRun.objects.filter(id=run.id).update(wake_at=timezone.now())
        q2_tasks.process_scheduled_workflows()

        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertTrue(run.data.get("woke"))

    def test_immediate_event_with_false_condition_does_not_fire(self):
        obj = ITImmediate.objects.create(flag=False)
        ct = ContentType.objects.get_for_model(ITImmediate)
        ev = Event.objects.get(model_type=ct, entity_id=str(obj.pk), event_name="immediate_evt")

        # Event row exists but should not be processed
        self.assertEqual(ev.status, EventStatus.PENDING)

    def test_scheduled_event_in_future_not_due_yet(self):
        future = timezone.now() + timedelta(hours=1)
        obj = ITScheduled.objects.create(due_at=future)

        ct = ContentType.objects.get_for_model(ITScheduled)
        ev = Event.objects.get(model_type=ct, entity_id=str(obj.pk))
        self.assertEqual(ev.status, EventStatus.PENDING)

        stats = q2_tasks.poll_due_events(hours=24)
        # nothing processed because it's still in the future
        self.assertEqual(stats["events_processed"], 0)

        ev.refresh_from_db()
        self.assertEqual(ev.status, EventStatus.PENDING)

    def test_scheduled_event_exact_now_triggers(self):
        now = timezone.now()
        obj = ITScheduled.objects.create(due_at=now)

        ct = ContentType.objects.get_for_model(ITScheduled)
        ev = Event.objects.get(model_type=ct, entity_id=str(obj.pk))
        self.assertEqual(ev.status, EventStatus.PENDING)

        stats = q2_tasks.poll_due_events(hours=24)
        self.assertGreaterEqual(stats["events_processed"], 1)

        ev.refresh_from_db()
        self.assertEqual(ev.status, EventStatus.PROCESSED)

    def test_waiter_does_not_advance_without_signal(self):
        run = engine.start("waiter")
        run.refresh_from_db()
        # Should be stuck in WAITING until a signal arrives
        self.assertEqual(run.status, WorkflowStatus.WAITING)
        self.assertFalse(run.data.get("got_event"))

    def test_sleep_workflow_not_prematurely_completed(self):
        run = engine.start("sleeper")
        engine.execute_step(run.id, "start")
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.WAITING)
        # run the scheduler *before* wake_at is due
        WorkflowRun.objects.filter(id=run.id).update(wake_at=timezone.now() + timedelta(hours=1))
        q2_tasks.process_scheduled_workflows()
        run.refresh_from_db()
        # Should still be WAITING
        self.assertEqual(run.status, WorkflowStatus.WAITING)

    def test_multiple_due_scheduled_events_processed(self):
        past = timezone.now() - timedelta(minutes=1)
        objs = [ITScheduled.objects.create(due_at=past) for _ in range(3)]
        stats = q2_tasks.poll_due_events(hours=24)
        self.assertGreaterEqual(stats["events_processed"], 3)
        for obj in objs:
            ev = Event.objects.get(entity_id=str(obj.pk))
            self.assertEqual(ev.status, EventStatus.PROCESSED)

    def test_immediate_event_invalid_is_not_processed_by_poller(self):
        # Create an immediate event that is invalid (flag=False)
        obj = ITImmediate.objects.create(flag=False)

        from django.contrib.contenttypes.models import ContentType
        from ..events.models import Event, EventStatus
        ct = ContentType.objects.get_for_model(ITImmediate)
        ev = Event.objects.get(model_type=ct, entity_id=str(obj.pk), event_name="immediate_evt")

        # Sanity: it's pending and invalid
        self.assertEqual(ev.status, EventStatus.PENDING)
        self.assertFalse(ev.is_valid)

        # Run poller (which previously would have incorrectly processed it)
        stats = q2_tasks.poll_due_events(hours=24)

        ev.refresh_from_db()
        # Should remain pending because is_valid is False
        self.assertEqual(ev.status, EventStatus.PENDING)
