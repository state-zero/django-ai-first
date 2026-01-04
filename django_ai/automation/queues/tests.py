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
                
                self.context.got_event = True
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
                
                if not self.context.slept_once:
                    self.context.slept_once = True
                    return sleep(timedelta(minutes=5))
                return goto(self.after)

            @step()
            def after(self):
                
                self.context.woke = True
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


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class TestDebounce(TransactionTestCase):
    """Tests for the debounce mechanism using the DebouncedTask model."""

    def setUp(self):
        from .models import DebouncedTask
        self.executor = SynchronousExecutor()
        engine.set_executor(self.executor)
        # Clean up any leftover debounced tasks
        DebouncedTask.objects.all().delete()

    def test_debounce_single_call_executes_after_delay(self):
        """A single debounced task executes after the delay."""
        from .models import DebouncedTask
        from freezegun import freeze_time

        initial_time = timezone.now()

        with freeze_time(initial_time) as frozen_time:
            # Debounce a task
            self.executor.debounce_task(
                "test-key",
                "poll_due_events",
                24,
                delay=timedelta(seconds=30),
                event_id=1,
            )

            # Task should exist in database but not executed
            self.assertTrue(DebouncedTask.objects.filter(debounce_key="test-key").exists())
            task = DebouncedTask.objects.get(debounce_key="test-key")
            self.assertEqual(task.task_name, "poll_due_events")
            self.assertEqual(task.task_args, (24,))

            # Process at current time - nothing due yet
            self.executor.process_debounced_tasks(timezone.now())
            self.assertTrue(DebouncedTask.objects.filter(debounce_key="test-key").exists())

            # Advance time past the delay
            frozen_time.tick(delta=timedelta(seconds=31))

            # Process after delay - task should be executed and removed from database
            self.executor.process_debounced_tasks(timezone.now())
            self.assertFalse(DebouncedTask.objects.filter(debounce_key="test-key").exists())

    def test_debounce_multiple_calls_accumulate_events(self):
        """Multiple debounced calls with same key accumulate events."""
        from .models import DebouncedTask

        # First call
        self.executor.debounce_task(
            "test-key",
            "poll_due_events",
            24,
            delay=timedelta(seconds=30),
            event_id=1,
        )

        # Second call - should push delay forward and accumulate event
        self.executor.debounce_task(
            "test-key",
            "poll_due_events",
            24,
            delay=timedelta(seconds=30),
            event_id=2,
        )

        # Third call
        self.executor.debounce_task(
            "test-key",
            "poll_due_events",
            24,
            delay=timedelta(seconds=30),
            event_id=3,
        )

        # Check accumulated events in database
        task = DebouncedTask.objects.get(debounce_key="test-key")
        self.assertEqual(task.event_ids, [1, 2, 3])

    def test_debounce_resets_scheduled_time(self):
        """Each debounce call resets the scheduled time."""
        from .models import DebouncedTask

        # First call
        self.executor.debounce_task(
            "test-key",
            "poll_due_events",
            24,
            delay=timedelta(seconds=30),
            event_id=1,
        )

        first_scheduled = DebouncedTask.objects.get(debounce_key="test-key").scheduled_at

        # Wait a bit (simulate time passing)
        import time
        time.sleep(0.01)

        # Second call
        self.executor.debounce_task(
            "test-key",
            "poll_due_events",
            24,
            delay=timedelta(seconds=30),
            event_id=2,
        )

        second_scheduled = DebouncedTask.objects.get(debounce_key="test-key").scheduled_at

        # Second scheduled time should be later
        self.assertGreater(second_scheduled, first_scheduled)

    def test_debounce_different_keys_independent(self):
        """Different debounce keys are tracked independently."""
        from .models import DebouncedTask

        self.executor.debounce_task(
            "key-1",
            "poll_due_events",
            24,
            delay=timedelta(seconds=30),
            event_id=1,
        )

        self.executor.debounce_task(
            "key-2",
            "cleanup_old_events",
            7,
            delay=timedelta(seconds=30),
            event_id=2,
        )

        self.assertEqual(DebouncedTask.objects.count(), 2)
        self.assertTrue(DebouncedTask.objects.filter(debounce_key="key-1").exists())
        self.assertTrue(DebouncedTask.objects.filter(debounce_key="key-2").exists())

        # Check they have different task names
        self.assertEqual(
            DebouncedTask.objects.get(debounce_key="key-1").task_name, "poll_due_events"
        )
        self.assertEqual(
            DebouncedTask.objects.get(debounce_key="key-2").task_name, "cleanup_old_events"
        )

    def test_debounce_duplicate_event_ids_not_added(self):
        """Same event ID is not added multiple times."""
        from .models import DebouncedTask

        self.executor.debounce_task(
            "test-key",
            "poll_due_events",
            24,
            delay=timedelta(seconds=30),
            event_id=1,
        )

        # Same event ID again
        self.executor.debounce_task(
            "test-key",
            "poll_due_events",
            24,
            delay=timedelta(seconds=30),
            event_id=1,
        )

        task = DebouncedTask.objects.get(debounce_key="test-key")
        self.assertEqual(task.event_ids, [1])  # Not [1, 1]


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class TestDebouncedWorkflows(TransactionTestCase):
    """Tests for debounced event workflows."""

    def setUp(self):
        self.executor = SynchronousExecutor()
        engine.set_executor(self.executor)

        from ..workflows.core import _workflows, _event_workflows
        from ..events.callbacks import callback_registry

        _workflows.clear()
        _event_workflows.clear()
        callback_registry.clear()

        from ..workflows.integration import handle_event_for_workflows
        callback_registry.register(
            handle_event_for_workflows, event_name="*", namespace="*"
        )

    def test_debounced_workflow_validation_requires_events_param(self):
        """Workflow with debounce must have create_context(events=...)."""
        with self.assertRaises(ValueError) as cm:
            @event_workflow(
                event_name="test_evt",
                debounce=timedelta(seconds=30),
            )
            class BadWorkflow:
                class Context(BaseModel):
                    count: int = 0

                @classmethod
                def create_context(cls, event=None):  # Wrong: should be events
                    return cls.Context()

                @step(start=True)
                def start(self):
                    return complete()

        self.assertIn("events", str(cm.exception))

    def test_debounced_workflow_accumulates_events(self):
        """Debounced workflow receives all events when it fires."""
        received_events = []

        @event_workflow(
            event_name="debounce_test_evt",
            debounce=timedelta(seconds=30),
            debounce_key="{entity.id}",
        )
        class DebouncedWF:
            class Context(BaseModel):
                event_count: int = 0

            @classmethod
            def create_context(cls, events=None):
                received_events.extend(events or [])
                return cls.Context(event_count=len(events or []))

            @step(start=True)
            def start(self):
                return complete()

        with time_machine() as tm:
            # Create 3 objects, each with its own event (different entity_ids)
            ct = ContentType.objects.get_for_model(ITImmediate)
            objs = [ITImmediate.objects.create(flag=True) for _ in range(3)]

            for obj in objs:
                ev = Event.objects.create(
                    model_type=ct,
                    entity_id=str(obj.pk),
                    event_name="debounce_test_evt",
                    status=EventStatus.PROCESSED,
                )
                # Trigger the event handling
                engine.handle_event_occurred(ev)

            # No workflow started yet (debounced) - but we have 3 different keys
            # Each entity has its own debounce key, so we should have 3 pending
            runs = WorkflowRun.objects.filter(name__contains="debounce_test_evt")
            self.assertEqual(runs.count(), 0)

            # Advance past debounce delay
            tm.advance(seconds=31)

            # Now 3 workflows should have started (one per entity)
            runs = WorkflowRun.objects.filter(name__contains="debounce_test_evt")
            self.assertEqual(runs.count(), 3)

    def test_debounced_workflow_same_key_accumulates(self):
        """Events with same debounce key accumulate into one workflow."""
        received_events = []

        @event_workflow(
            event_name="debounce_same_key_evt",
            debounce=timedelta(seconds=30),
            debounce_key="fixed_key",  # Same key for all events
        )
        class DebouncedSameKeyWF:
            class Context(BaseModel):
                event_count: int = 0

            @classmethod
            def create_context(cls, events=None):
                received_events.extend(events or [])
                return cls.Context(event_count=len(events or []))

            @step(start=True)
            def start(self):
                return complete()

        with time_machine() as tm:
            ct = ContentType.objects.get_for_model(ITImmediate)

            # Create 3 events with different entities but same debounce key
            for i in range(3):
                obj = ITImmediate.objects.create(flag=True)
                ev = Event.objects.create(
                    model_type=ct,
                    entity_id=str(obj.pk),
                    event_name="debounce_same_key_evt",
                    status=EventStatus.PROCESSED,
                )
                engine.handle_event_occurred(ev)

            # No workflow started yet (debounced)
            runs = WorkflowRun.objects.filter(name__contains="debounce_same_key_evt")
            self.assertEqual(runs.count(), 0)

            # Advance past debounce delay
            tm.advance(seconds=31)

            # Only ONE workflow should have started (same key)
            runs = WorkflowRun.objects.filter(name__contains="debounce_same_key_evt")
            self.assertEqual(runs.count(), 1)
            # Should have received all 3 events
            self.assertEqual(runs.first().data["event_count"], 3)
            self.assertEqual(len(received_events), 3)


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class TestDebouncedHandlers(TransactionTestCase):
    """Tests for debounced agent handlers."""

    def setUp(self):
        self.executor = SynchronousExecutor()
        engine.set_executor(self.executor)

        from ..agents.core import agent_engine, _agents, _agent_handlers
        agent_engine.set_executor(self.executor)

        _agents.clear()
        _agent_handlers.clear()

        from ..workflows.core import _workflows, _event_workflows
        from ..events.callbacks import callback_registry

        _workflows.clear()
        _event_workflows.clear()
        callback_registry.clear()

    def test_debounced_handler_validation_requires_events_param(self):
        """Handler with debounce must have events parameter."""
        from ..agents.core import agent, handler

        with self.assertRaises(ValueError) as cm:
            @agent(name="bad_agent", spawn_on="spawn_evt", match={"id": "{{ context.entity_id }}"})
            class BadAgent:
                class Context(BaseModel):
                    entity_id: int

                @classmethod
                def create_context(cls, event):
                    return cls.Context(entity_id=event.entity_id)

                @handler("update_evt", debounce=timedelta(seconds=30))
                def handle_update(self):  # Wrong: should have events param
                    pass

        self.assertIn("events", str(cm.exception))

    def test_debounced_handler_accumulates_events(self):
        """Debounced handler receives all events when it fires."""
        from ..agents.core import agent, handler, agent_engine, _register_event_handler
        from ..agents.models import AgentRun, AgentStatus
        from tests.models import TestBooking

        received_events = []

        @agent(
            name="debounce_handler_agent",
            spawn_on="booking_created",
            match={"id": "{{ context.booking_id }}"}
        )
        class DebounceHandlerAgent:
            class Context(BaseModel):
                booking_id: int = 0
                update_count: int = 0

            @classmethod
            def create_context(cls, event):
                return cls.Context(booking_id=event.entity_id)

            @handler(
                "booking_updated",
                debounce=timedelta(seconds=30),
                debounce_key="{entity.id}",
            )
            def handle_updates(self, events):
                received_events.extend(events)
                self.context.update_count = len(events)

        # Register the handler callbacks
        for h in DebounceHandlerAgent._handlers:
            _register_event_handler("debounce_handler_agent", h, DebounceHandlerAgent)

        with time_machine() as tm:
            ct = ContentType.objects.get_for_model(TestBooking)

            # Create a booking and spawn the agent
            booking = TestBooking.objects.create(
                guest_name="Test Guest",
                checkin_date=timezone.now() + timedelta(days=1),
                checkout_date=timezone.now() + timedelta(days=3),
                status="pending",
            )
            spawn_event = Event.objects.create(
                model_type=ct,
                entity_id=str(booking.pk),
                event_name="booking_created",
                status=EventStatus.PROCESSED,
            )
            agent_run = agent_engine.spawn("debounce_handler_agent", spawn_event)
            self.assertIsNotNone(agent_run)

            # Now fire multiple update events quickly
            for i in range(3):
                update_event = Event.objects.create(
                    model_type=ct,
                    entity_id=str(booking.pk),
                    event_name="booking_updated",
                    namespace=f"update_{i}",  # Different namespace to avoid unique constraint
                    status=EventStatus.PROCESSED,
                )
                agent_engine.schedule_handler(
                    "debounce_handler_agent",
                    "handle_updates",
                    update_event,
                )

            # Handler should not have executed yet (debounced)
            self.assertEqual(len(received_events), 0)

            # Advance past debounce delay
            tm.advance(seconds=31)

            # Now handler should have executed with all 3 events
            self.assertEqual(len(received_events), 3)
            agent_run.refresh_from_db()
            self.assertEqual(agent_run.context["update_count"], 3)
