"""Tests for the time manipulation utility."""

from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Optional
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from pydantic import BaseModel

from django_ai.automation.workflows.core import (
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
from django_ai.automation.workflows.models import WorkflowRun, WorkflowStatus
from django_ai.automation.queues.sync_executor import SynchronousExecutor
from django_ai.automation.testing import time_machine, TimeMachine
from django_ai.automation.testing.time_machine import with_time_machine


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class TestTimeMachineBasics(TransactionTestCase):
    """Test basic time manipulation functionality."""

    def test_freezes_time(self):
        """Time should be frozen at the start time."""
        start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt_timezone.utc)

        with time_machine(start=start) as tm:
            self.assertEqual(timezone.now(), start)
            # Time doesn't advance on its own
            self.assertEqual(timezone.now(), start)

    def test_advance_by_delta(self):
        """Time can be advanced by a timedelta."""
        start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt_timezone.utc)

        with time_machine(start=start) as tm:
            tm.advance(hours=1, minutes=30)
            expected = start + timedelta(hours=1, minutes=30)
            self.assertEqual(timezone.now(), expected)

    def test_advance_by_explicit_delta(self):
        """Time can be advanced by an explicit timedelta object."""
        start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt_timezone.utc)

        with time_machine(start=start) as tm:
            tm.advance(delta=timedelta(days=1))
            expected = start + timedelta(days=1)
            self.assertEqual(timezone.now(), expected)

    def test_advance_to_specific_time(self):
        """Time can be advanced to a specific datetime."""
        start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt_timezone.utc)
        target = datetime(2024, 6, 16, 9, 0, 0, tzinfo=dt_timezone.utc)

        with time_machine(start=start) as tm:
            tm.advance_to(target)
            self.assertEqual(timezone.now(), target)

    def test_advance_to_past_raises_error(self):
        """Cannot advance time backwards."""
        start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt_timezone.utc)
        past = datetime(2024, 6, 14, 12, 0, 0, tzinfo=dt_timezone.utc)

        with time_machine(start=start) as tm:
            with self.assertRaises(ValueError):
                tm.advance_to(past)

    def test_freeze_at_new_time(self):
        """Can freeze time at a new point."""
        start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt_timezone.utc)
        new_time = datetime(2024, 7, 1, 0, 0, 0, tzinfo=dt_timezone.utc)

        with time_machine(start=start) as tm:
            tm.freeze(at=new_time)
            self.assertEqual(timezone.now(), new_time)

    def test_restores_time_after_context(self):
        """Real time is restored after exiting context."""
        start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt_timezone.utc)

        with time_machine(start=start):
            self.assertEqual(timezone.now(), start)

        # After context, should be back to real time (approximately now)
        now = timezone.now()
        self.assertNotEqual(now, start)
        # Should be within last few seconds
        self.assertLess(abs((now - timezone.now()).total_seconds()), 1)

    def test_stats_tracking(self):
        """Processing stats are tracked cumulatively."""
        with time_machine() as tm:
            # No processing yet
            self.assertEqual(tm.stats.events_processed, 0)
            self.assertEqual(tm.stats.workflows_woken, 0)

            # Advance time (triggers processing but nothing to process)
            tm.advance(hours=1)
            # Stats should still be valid (may be 0 if nothing due)
            self.assertIsInstance(tm.stats.events_processed, int)
            self.assertIsInstance(tm.stats.workflows_woken, int)


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class TestTimeMachineWithWorkflows(TransactionTestCase):
    """Test time machine integration with workflow system."""

    def setUp(self):
        engine.set_executor(SynchronousExecutor())
        # Clear registries
        from django_ai.automation.workflows.core import _workflows, _event_workflows
        from django_ai.automation.events.callbacks import callback_registry

        _workflows.clear()
        _event_workflows.clear()
        callback_registry.clear()

        from django_ai.automation.workflows.integration import handle_event_for_workflows

        callback_registry.register(
            handle_event_for_workflows, event_name="*", namespace="*"
        )

        # Define a sleeper workflow - uses slept flag to check if already slept
        # (the same step is re-run after waking from sleep)
        @workflow("test_sleeper")
        class TestSleeperWF:
            class Context(BaseModel):
                slept: bool = False
                woke: bool = False

            @classmethod
            def create_context(cls):
                return cls.Context()

            @step(start=True)
            def start(self):
                ctx = get_context()
                if not ctx.slept:
                    ctx.slept = True
                    return sleep(timedelta(minutes=30))
                return goto(self.after_sleep)

            @step()
            def after_sleep(self):
                ctx = get_context()
                ctx.woke = True
                return complete()

    def test_workflow_sleeps_and_wakes_with_time_advance(self):
        """Workflow should wake up when time is advanced past sleep duration."""
        with time_machine() as tm:
            run = engine.start("test_sleeper")
            engine.execute_step(run.id, "start")

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.WAITING)
            self.assertTrue(run.data.get("slept"))
            self.assertFalse(run.data.get("woke"))

            # Advance time past the 30 minute sleep
            stats = tm.advance(minutes=31)
            self.assertGreaterEqual(stats.workflows_woken, 1)

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)
            self.assertTrue(run.data.get("woke"))

    def test_workflow_does_not_wake_before_sleep_duration(self):
        """Workflow should stay sleeping if time hasn't advanced enough."""
        with time_machine() as tm:
            run = engine.start("test_sleeper")
            engine.execute_step(run.id, "start")

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.WAITING)

            # Advance only 10 minutes (sleep is 30 minutes)
            tm.advance(minutes=10)

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.WAITING)
            self.assertFalse(run.data.get("woke"))

    def test_auto_process_disabled(self):
        """With auto_process=False, must manually call process()."""
        with time_machine(auto_process=False) as tm:
            run = engine.start("test_sleeper")
            engine.execute_step(run.id, "start")

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.WAITING)

            # Advance time but don't process
            tm.advance(minutes=31)

            # Should still be waiting (not auto-processed)
            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.WAITING)

            # Now manually process
            tm.process()

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)

    def test_run_until_idle(self):
        """run_until_idle processes all currently due work recursively."""
        # Define a workflow that chains multiple steps with sleeps
        # Each step checks a flag to know whether to sleep or proceed
        @workflow("multi_sleep")
        class MultiSleepWF:
            class Context(BaseModel):
                count: int = 0
                slept_step1: bool = False
                slept_step2: bool = False

            @classmethod
            def create_context(cls):
                return cls.Context()

            @step(start=True)
            def step1(self):
                ctx = get_context()
                if not ctx.slept_step1:
                    ctx.count = 1
                    ctx.slept_step1 = True
                    return sleep(timedelta(seconds=1))
                return goto(self.step2)

            @step()
            def step2(self):
                ctx = get_context()
                if not ctx.slept_step2:
                    ctx.count = 2
                    ctx.slept_step2 = True
                    return sleep(timedelta(seconds=1))
                return goto(self.step3)

            @step()
            def step3(self):
                ctx = get_context()
                ctx.count = 3
                return complete()

        with time_machine() as tm:
            run = engine.start("multi_sleep")
            engine.execute_step(run.id, "step1")

            # First advance to get past sleep1
            tm.advance(seconds=2)
            tm.run_until_idle()

            run.refresh_from_db()
            # Should be waiting on sleep2 now
            self.assertEqual(run.status, WorkflowStatus.WAITING)
            self.assertEqual(run.data.get("count"), 2)

            # Another advance to get past sleep2
            tm.advance(seconds=2)
            tm.run_until_idle()

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)
            self.assertEqual(run.data.get("count"), 3)

    def test_run_workflow_to_completion(self):
        """run_workflow_to_completion automatically advances time."""
        # Define a workflow with multiple long sleeps
        @workflow("long_sleeper")
        class LongSleeperWF:
            class Context(BaseModel):
                slept1: bool = False
                slept2: bool = False
                done: bool = False

            @classmethod
            def create_context(cls):
                return cls.Context()

            @step(start=True)
            def start(self):
                ctx = get_context()
                if not ctx.slept1:
                    ctx.slept1 = True
                    return sleep(timedelta(hours=1))
                return goto(self.middle)

            @step()
            def middle(self):
                ctx = get_context()
                if not ctx.slept2:
                    ctx.slept2 = True
                    return sleep(timedelta(hours=2))
                return goto(self.finish)

            @step()
            def finish(self):
                ctx = get_context()
                ctx.done = True
                return complete()

        with time_machine() as tm:
            run = engine.start("long_sleeper")
            engine.execute_step(run.id, "start")

            # Use run_workflow_to_completion - it should auto-advance
            final_run = tm.run_workflow_to_completion(run.id)

            self.assertEqual(final_run.status, WorkflowStatus.COMPLETED)
            self.assertTrue(final_run.data.get("done"))

    def test_run_workflow_to_completion_timeout(self):
        """run_workflow_to_completion raises TimeoutError if workflow gets stuck."""
        # Define a workflow that sleeps forever
        @workflow("stuck_sleeper")
        class StuckSleeperWF:
            class Context(BaseModel):
                pass

            @classmethod
            def create_context(cls):
                return cls.Context()

            @step(start=True)
            def start(self):
                # Always sleep - never complete
                return sleep(timedelta(days=1))

        with time_machine() as tm:
            run = engine.start("stuck_sleeper")
            engine.execute_step(run.id, "start")

            with self.assertRaises(TimeoutError):
                tm.run_workflow_to_completion(
                    run.id,
                    max_advance=timedelta(hours=1),  # Only allow 1 hour
                )


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class TestTimeMachineDecorator(TransactionTestCase):
    """Test the decorator form of time machine."""

    def setUp(self):
        engine.set_executor(SynchronousExecutor())
        from django_ai.automation.workflows.core import _workflows, _event_workflows
        from django_ai.automation.events.callbacks import callback_registry

        _workflows.clear()
        _event_workflows.clear()
        callback_registry.clear()

    @with_time_machine(start=datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt_timezone.utc))
    def test_decorator_provides_time_machine(self, tm):
        """Decorator should provide TimeMachine as argument."""
        self.assertIsInstance(tm, TimeMachine)
        self.assertEqual(
            timezone.now(),
            datetime(2024, 6, 15, 12, 0, 0, tzinfo=dt_timezone.utc)
        )

    @with_time_machine()
    def test_decorator_allows_time_advance(self, tm):
        """Decorator's time machine should support advancing."""
        start = timezone.now()
        tm.advance(hours=2)
        self.assertEqual(timezone.now(), start + timedelta(hours=2))


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class TestTimeMachineBackgroundLoop(TransactionTestCase):
    """Test background processing loop."""

    def setUp(self):
        engine.set_executor(SynchronousExecutor())
        from django_ai.automation.workflows.core import _workflows, _event_workflows
        from django_ai.automation.events.callbacks import callback_registry

        _workflows.clear()
        _event_workflows.clear()
        callback_registry.clear()

        @workflow("bg_sleeper")
        class BgSleeperWF:
            class Context(BaseModel):
                woke: bool = False

            @classmethod
            def create_context(cls):
                return cls.Context()

            @step(start=True)
            def start(self):
                return sleep(timedelta(milliseconds=10))

            @step()
            def after(self):
                ctx = get_context()
                ctx.woke = True
                return complete()

    def test_background_loop_starts_and_stops(self):
        """Background loop should start and stop cleanly."""
        with time_machine(background_loop=True, tick_interval=0.001) as tm:
            self.assertIsNotNone(tm._background_thread)
            self.assertTrue(tm._background_thread.is_alive())

        # After context exit, thread should be stopped
        self.assertTrue(tm._stop_event.is_set())

    def test_cannot_start_background_loop_twice(self):
        """Starting background loop twice should raise error."""
        with time_machine() as tm:
            tm.start_background_loop()
            with self.assertRaises(RuntimeError):
                tm.start_background_loop()


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class TestTimeMachineEventWorkflowIntegration(TransactionTestCase):
    """
    Test time machine with scheduled events, event workflows with offsets,
    and workflows that trigger their own events.
    """

    def setUp(self):
        engine.set_executor(SynchronousExecutor())
        from django_ai.automation.workflows.core import _workflows, _event_workflows
        from django_ai.automation.events.callbacks import callback_registry

        _workflows.clear()
        _event_workflows.clear()
        callback_registry.clear()

        # Register the event->workflow integration
        from django_ai.automation.workflows.integration import handle_event_for_workflows
        callback_registry.register(
            handle_event_for_workflows, event_name="*", namespace="*"
        )

    def test_scheduled_event_triggers_workflow_with_offset(self):
        """
        A scheduled event in the future should trigger an event workflow
        with an offset when time advances appropriately.
        """
        from tests.models import ITScheduled
        from django.contrib.contenttypes.models import ContentType
        from django_ai.automation.events.models import Event, EventStatus

        # Define an event workflow with a 15-minute offset
        @event_workflow(event_name="scheduled_evt", offset=timedelta(minutes=15))
        class ScheduledWithOffsetWF:
            class Context(BaseModel):
                started: bool = False
                completed: bool = False

            @classmethod
            def create_context(cls, event=None):
                return cls.Context(started=True)

            @step(start=True)
            def start(self):
                ctx = get_context()
                ctx.completed = True
                return complete()

        with time_machine() as tm:
            # Create a scheduled event 1 hour in the future
            future_time = timezone.now() + timedelta(hours=1)
            obj = ITScheduled.objects.create(due_at=future_time)

            ct = ContentType.objects.get_for_model(ITScheduled)
            ev = Event.objects.get(
                model_type=ct, entity_id=str(obj.pk), event_name="scheduled_evt"
            )

            # Event should be pending
            self.assertEqual(ev.status, EventStatus.PENDING)

            # Advance to just before the event time - nothing should happen
            tm.advance(minutes=59)
            ev.refresh_from_db()
            self.assertEqual(ev.status, EventStatus.PENDING)

            # No workflow should exist yet
            runs = WorkflowRun.objects.filter(triggered_by_event_id=ev.id)
            self.assertEqual(runs.count(), 0)

            # Advance past the event time - event should be processed
            tm.advance(minutes=2)  # Now at 1h1m
            ev.refresh_from_db()
            self.assertEqual(ev.status, EventStatus.PROCESSED)

            # Workflow should be created but waiting (due to 15min offset)
            run = WorkflowRun.objects.filter(
                triggered_by_event_id=ev.id,
                name=f"event:scheduled_evt:{timedelta(minutes=15)}"
            ).first()
            self.assertIsNotNone(run)
            self.assertEqual(run.status, WorkflowStatus.WAITING)
            self.assertIsNotNone(run.wake_at)

            # Advance past the offset
            tm.advance(minutes=16)  # Now at 1h17m

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)
            self.assertTrue(run.data.get("completed"))

    def test_workflow_triggers_event_which_triggers_another_workflow(self):
        """
        A workflow can create a model that triggers an event,
        which in turn triggers another workflow.
        """
        from tests.models import ITImmediate
        from django.contrib.contenttypes.models import ContentType
        from django_ai.automation.events.models import Event, EventStatus

        created_obj_ids = []

        # Define workflow that creates a model (which triggers an event)
        @workflow("model_creator")
        class ModelCreatorWF:
            class Context(BaseModel):
                created_model_id: Optional[int] = None

            @classmethod
            def create_context(cls):
                return cls.Context()

            @step(start=True)
            def create_model(self):
                ctx = get_context()
                # Create a model that will fire an immediate event
                obj = ITImmediate.objects.create(flag=True)
                ctx.created_model_id = obj.id
                created_obj_ids.append(obj.id)
                return complete()

        # Define event workflow triggered by immediate_evt
        @event_workflow(event_name="immediate_evt")
        class ImmediateHandlerWF:
            class Context(BaseModel):
                handled: bool = False
                entity_id: Optional[str] = None

            @classmethod
            def create_context(cls, event=None):
                return cls.Context(entity_id=str(event.entity_id) if event else None)

            @step(start=True)
            def handle(self):
                ctx = get_context()
                ctx.handled = True
                return complete()

        with time_machine() as tm:
            # Start the model creator workflow
            run = engine.start("model_creator")
            engine.execute_step(run.id, "create_model")

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)
            self.assertIsNotNone(run.data.get("created_model_id"))

            # The immediate event should have been processed
            ct = ContentType.objects.get_for_model(ITImmediate)
            ev = Event.objects.get(
                model_type=ct,
                entity_id=str(created_obj_ids[0]),
                event_name="immediate_evt"
            )
            self.assertEqual(ev.status, EventStatus.PROCESSED)

            # The event workflow should have run
            handler_run = WorkflowRun.objects.filter(
                triggered_by_event_id=ev.id,
                name__startswith="event:immediate_evt"
            ).first()
            self.assertIsNotNone(handler_run)
            self.assertEqual(handler_run.status, WorkflowStatus.COMPLETED)
            self.assertTrue(handler_run.data.get("handled"))

    def test_chained_scheduled_events_with_multiple_offsets(self):
        """
        Test a chain: scheduled event → workflow with offset → creates another
        scheduled event → another workflow.
        """
        from tests.models import ITScheduled
        from django.contrib.contenttypes.models import ContentType
        from django_ai.automation.events.models import Event, EventStatus

        followup_ids = []

        # First workflow: triggered by scheduled_evt with 10min offset
        # Creates another scheduled event for 30min later
        @event_workflow(event_name="scheduled_evt", offset=timedelta(minutes=10))
        class FirstHandlerWF:
            class Context(BaseModel):
                processed: bool = False
                followup_id: Optional[int] = None

            @classmethod
            def create_context(cls, event=None):
                return cls.Context()

            @step(start=True)
            def process_and_create_followup(self):
                ctx = get_context()
                ctx.processed = True
                # Create a followup scheduled event 30 minutes from now
                followup_time = timezone.now() + timedelta(minutes=30)
                followup = ITScheduled.objects.create(due_at=followup_time)
                ctx.followup_id = followup.id
                followup_ids.append(followup.id)
                return complete()

        # Second workflow: triggered by the followup's scheduled_evt (no offset)
        # We need a different event name or we'd conflict, so let's use a marker
        second_handler_completed = []

        @event_workflow(event_name="scheduled_evt")
        class SecondHandlerWF:
            class Context(BaseModel):
                final: bool = False
                entity_id: Optional[str] = None

            @classmethod
            def create_context(cls, event=None):
                return cls.Context(entity_id=str(event.entity_id) if event else None)

            @step(start=True)
            def finalize(self):
                ctx = get_context()
                ctx.final = True
                second_handler_completed.append(ctx.entity_id)
                return complete()

        with time_machine() as tm:
            # Create initial scheduled event 1 hour from now
            initial_time = timezone.now() + timedelta(hours=1)
            obj = ITScheduled.objects.create(due_at=initial_time)
            initial_id = obj.id

            ct = ContentType.objects.get_for_model(ITScheduled)

            # Advance to 1h1m - event fires, first workflow starts but waits (10min offset)
            tm.advance(hours=1, minutes=1)

            ev = Event.objects.get(
                model_type=ct, entity_id=str(initial_id), event_name="scheduled_evt"
            )
            self.assertEqual(ev.status, EventStatus.PROCESSED)

            first_run = WorkflowRun.objects.filter(
                triggered_by_event_id=ev.id,
                name=f"event:scheduled_evt:{timedelta(minutes=10)}"
            ).first()
            self.assertIsNotNone(first_run)
            self.assertEqual(first_run.status, WorkflowStatus.WAITING)

            # Advance 11 more minutes - first workflow completes, creates followup
            tm.advance(minutes=11)  # Now at 1h12m

            first_run.refresh_from_db()
            self.assertEqual(first_run.status, WorkflowStatus.COMPLETED)
            self.assertTrue(first_run.data.get("processed"))
            self.assertIsNotNone(first_run.data.get("followup_id"))

            # Followup event should exist but be pending (30min in future)
            followup_ev = Event.objects.get(
                model_type=ct,
                entity_id=str(followup_ids[0]),
                event_name="scheduled_evt"
            )
            self.assertEqual(followup_ev.status, EventStatus.PENDING)

            # Advance 31 more minutes - followup event fires, second workflow runs
            tm.advance(minutes=31)  # Now at 1h43m

            followup_ev.refresh_from_db()
            self.assertEqual(followup_ev.status, EventStatus.PROCESSED)

            # Second handler should have completed for the followup
            self.assertIn(str(followup_ids[0]), second_handler_completed)
