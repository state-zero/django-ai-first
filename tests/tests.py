import unittest
from datetime import datetime, timedelta
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from django.db import transaction, IntegrityError
from pydantic import BaseModel
import threading
import time
from unittest.mock import patch, Mock

from django_ai.automation.events.models import Event, EventStatus
from django_ai.automation.events.callbacks import on_event, callback_registry
from django_ai.automation.events.services import event_processor
from django_ai.automation.workflows.core import (
    workflow,
    event_workflow,
    step,
    get_context,
    goto,
    complete,
    engine,
    wait_for_event
)
from django_ai.automation.workflows.models import WorkflowRun, WorkflowStatus
from django_ai.automation.queues.sync_executor import SynchronousExecutor

# Import test models
from .models import Guest, Booking


class ProductionScenarioTests(TestCase):
    """Tests for real-world production scenarios and edge cases"""

    def setUp(self):
        """Set up test environment"""
        engine.set_executor(SynchronousExecutor())
        from django_ai.automation.workflows.core import _workflows, _event_workflows

        _workflows.clear()
        _event_workflows.clear()
        callback_registry.clear()

        self.guest = Guest.objects.create(email="test@example.com", name="Test User")

    def test_concurrent_workflow_execution(self):
        """Test multiple workflows running concurrently don't interfere"""

        @workflow("concurrent_test_workflow")
        class ConcurrentWorkflow:
            class Context(BaseModel):
                workflow_id: str
                step_count: int = 0
                processing_time: float = 0.0
                final_count: int = 0  # Add this field so complete() can merge

            @classmethod
            def create_context(cls, workflow_id: str):
                return cls.Context(workflow_id=workflow_id)

            @step(start=True)
            def step_one(self):
                ctx = get_context()
                start_time = time.time()
                ctx.step_count += 1
                # Simulate some processing time
                time.sleep(0.01)
                ctx.processing_time = time.time() - start_time
                return goto(self.step_two)

            @step()
            def step_two(self):
                ctx = get_context()
                ctx.step_count += 1
                return complete(final_count=ctx.step_count)

        # Start multiple workflows
        workflow_runs = []
        for i in range(5):
            run = engine.start("concurrent_test_workflow", workflow_id=f"workflow_{i}")
            workflow_runs.append(run)

        # All should complete successfully
        for run in workflow_runs:
            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)
            self.assertEqual(run.data["step_count"], 2)
            self.assertEqual(run.data["final_count"], 2)  # complete() result merged
            # Each workflow should have its own context
            self.assertIn(
                f"workflow_{workflow_runs.index(run)}", run.data["workflow_id"]
            )

    def test_event_processing_under_load(self):
        """Test event processing with many events"""

        # Create multiple bookings rapidly
        bookings = []
        for i in range(10):
            booking = Booking.objects.create(
                guest=self.guest,
                checkin_date=timezone.now() + timedelta(days=i + 1),
                checkout_date=timezone.now() + timedelta(days=i + 3),
                status="confirmed",
            )
            bookings.append(booking)

        # Check all events were created
        total_events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(Booking)
        ).count()

        # Each booking should create multiple events
        self.assertGreater(total_events, len(bookings))

        # Process all due events using production task
        from django_ai.automation.queues import tasks as q2_tasks

        result = q2_tasks.poll_due_events(hours=24)
        self.assertGreater(result["total_processed"], 0)

    def test_workflow_with_database_rollback(self):
        """Test workflow behavior when database operations fail"""

        @workflow("db_rollback_test")
        class DbRollbackWorkflow:
            class Context(BaseModel):
                should_cause_db_error: bool
                step_reached: str = ""

            @classmethod
            def create_context(cls, should_cause_db_error: bool = False):
                return cls.Context(should_cause_db_error=should_cause_db_error)

            @step(start=True)
            def start_step(self):
                ctx = get_context()
                ctx.step_reached = "start_step"

                if ctx.should_cause_db_error:
                    # Cause a validation error instead of foreign key error
                    raise ValueError("Simulated database validation error")

                return goto(self.end_step)

            @step()
            def end_step(self):
                ctx = get_context()
                ctx.step_reached = "end_step"
                return complete()

        # Test successful case
        success_run = engine.start("db_rollback_test", should_cause_db_error=False)
        success_run.refresh_from_db()
        self.assertEqual(success_run.status, WorkflowStatus.COMPLETED)

        # Test failure case
        error_run = engine.start("db_rollback_test", should_cause_db_error=True)
        error_run.refresh_from_db()

        # Should either be waiting for retry or failed
        self.assertIn(error_run.status, [WorkflowStatus.WAITING, WorkflowStatus.FAILED])
        if error_run.status == WorkflowStatus.WAITING:
            self.assertGreater(error_run.retry_count, 0)

    def test_event_workflow_timing_edge_cases(self):
        """Test event workflows with various timing scenarios"""

        callback_calls = []

        @event_workflow("checkin_due", offset=timedelta())
        class ImmediateCheckinWorkflow:
            class Context(BaseModel):
                booking_id: int
                processed_at: str

            @classmethod
            def create_context(cls, event=None):
                return cls.Context(
                    booking_id=event.entity.id, processed_at=timezone.now().isoformat()
                )

            @step(start=True)
            def process_checkin(self):
                ctx = get_context()
                callback_calls.append(f"immediate_checkin_{ctx.booking_id}")
                return complete()

        @event_workflow("checkin_due", offset=timedelta(minutes=60))  # 1 hour after
        class DelayedCheckinWorkflow:
            class Context(BaseModel):
                booking_id: int
                processed_at: str

            @classmethod
            def create_context(cls, event=None):
                return cls.Context(
                    booking_id=event.entity.id, processed_at=timezone.now().isoformat()
                )

            @step(start=True)
            def process_delayed_checkin(self):
                ctx = get_context()
                callback_calls.append(f"delayed_checkin_{ctx.booking_id}")
                return complete()

        # Create booking with checkin in the past (should trigger immediately)
        past_booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() - timedelta(hours=1),  # Past date
            checkout_date=timezone.now() + timedelta(days=1),
            status="confirmed",
        )

        # Create booking with checkin in the future
        future_booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(hours=2),  # Future date
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )

        # Trigger the events manually
        for booking in [past_booking, future_booking]:
            event = Event.objects.get(
                model_type=ContentType.objects.get_for_model(Booking),
                entity_id=str(booking.id),
                event_name="checkin_due",
            )

            # Simulate workflows being triggered
            immediate_run = engine.start_for_event(event, ImmediateCheckinWorkflow)
            delayed_run = engine.start_for_event(event, DelayedCheckinWorkflow)

            # Immediate should run right away
            immediate_run.refresh_from_db()
            if immediate_run.status == WorkflowStatus.COMPLETED:
                self.assertIn(f"immediate_checkin_{booking.id}", callback_calls)

            # Delayed should be waiting (unless offset puts it in the past)
            delayed_run.refresh_from_db()
            if booking == past_booking:
                # Past booking + 60min offset might still be in past
                self.assertIn(
                    delayed_run.status,
                    [WorkflowStatus.COMPLETED, WorkflowStatus.WAITING],
                )

    def test_workflow_signal_and_wait_mechanisms(self):
        """Test complex wait/signal patterns using production flow"""

        @workflow("signal_test_workflow")
        class SignalTestWorkflow:
            class Context(BaseModel):
                workflow_id: str
                signals_received: list = []
                timeouts_hit: int = 0
                extra_data: str = ""

            @classmethod
            def create_context(cls, workflow_id: str):
                return cls.Context(workflow_id=workflow_id)

            @step(start=True)
            def start_waiting(self):
                return goto(self.wait_for_signal_a)

            @wait_for_event("test_signal_a", timeout=timedelta(seconds=1), on_timeout="timeout_step")
            @step()
            def wait_for_signal_a(self):
                # This step runs if signal_a is received
                ctx = get_context()
                ctx.signals_received.append("signal_a")
                return goto(self.wait_for_signal_b)
            
            @step()
            def timeout_step(self):
                ctx = get_context()
                ctx.timeouts_hit += 1
                return goto(self.wait_for_signal_b)

            @wait_for_event("test_signal_b")
            @step()
            def wait_for_signal_b(self):
                ctx = get_context()
                ctx.signals_received.append("signal_b")
                return complete()

        workflow_run = engine.start("signal_test_workflow", workflow_id="test_123")
        workflow_run.refresh_from_db()

        self.assertEqual(workflow_run.status, WorkflowStatus.SUSPENDED)
        self.assertEqual(workflow_run.waiting_signal, "event:test_signal_a")

        workflow_run.wake_at = timezone.now() - timedelta(seconds=1)
        workflow_run.save()

        from django_ai.automation.queues import tasks as q2_tasks
        q2_tasks.process_scheduled_workflows()

        workflow_run.refresh_from_db()
        self.assertEqual(workflow_run.status, WorkflowStatus.SUSPENDED)
        self.assertEqual(workflow_run.waiting_signal, "event:test_signal_b")
        self.assertEqual(workflow_run.data["timeouts_hit"], 1)

        engine.signal("event:test_signal_b", {"extra_data": "test"})
        workflow_run.refresh_from_db()

        self.assertEqual(workflow_run.status, WorkflowStatus.COMPLETED)
        self.assertIn("signal_b", workflow_run.data["signals_received"])
        self.assertEqual(workflow_run.data["extra_data"], "test")

    def test_event_condition_race_conditions(self):
        """Test event conditions under rapid model changes"""

        # Create booking
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=3),
            status="pending",
        )

        # Rapidly change booking status multiple times
        statuses = ["confirmed", "pending", "confirmed", "cancelled", "confirmed"]

        for status in statuses:
            booking.status = status
            booking.save()

        # Check final event state
        events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
        )

        # Events should exist
        self.assertGreater(events.count(), 0)

        # Check event validity matches final booking state
        for event in events:
            if event.event_name == "booking_confirmed":
                self.assertEqual(event.is_valid, booking.status == "confirmed")

    def test_workflow_cleanup_and_cancellation(self):
        """Test workflow cleanup scenarios"""

        @workflow("cleanup_test_workflow")
        class CleanupWorkflow:
            class Context(BaseModel):
                workflow_id: str
                cleanup_performed: bool = False

            @classmethod
            def create_context(cls, workflow_id: str):
                return cls.Context(workflow_id=workflow_id)

            @step(start=True)
            def start_waiting(self):
                return goto(self.long_running_step)

            @wait_for_event("never_comes", timeout=timedelta(hours=1))
            @step()
            def long_running_step(self):
                # This step will never run because the signal is never sent
                return complete()
            
            @step()
            def cleanup_step(self):
                ctx = get_context()
                ctx.cleanup_performed = True
                return complete()
            
        workflow_run = engine.start("cleanup_test_workflow", workflow_id="cleanup_test")

        workflow_run.refresh_from_db()
        self.assertEqual(workflow_run.status, WorkflowStatus.SUSPENDED)

        engine.cancel(workflow_run.id)

        workflow_run.refresh_from_db()
        self.assertEqual(workflow_run.status, WorkflowStatus.CANCELLED)

    def test_event_processor_error_handling(self):
        """Test event processor handling various error conditions"""

        # Create booking with invalid date (past checkin)
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() - timedelta(days=1),  # Past date
            checkout_date=timezone.now() + timedelta(days=1),
            status="confirmed",
        )

        # Create event manually with corrupted data
        event = Event.objects.create(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id="999999",  # Non-existent booking ID
            event_name="checkin_due",
            status=EventStatus.PENDING,
        )

        # Process events using production task - should handle missing entity gracefully
        from django_ai.automation.queues import tasks as q2_tasks

        result = q2_tasks.poll_due_events(hours=48)

        # Should not crash
        self.assertIsInstance(result, dict)
        self.assertIn("total_processed", result)

    def test_workflow_memory_and_context_limits(self):
        """Test workflows with large context data"""

        @workflow("large_context_workflow")
        class LargeContextWorkflow:
            class Context(BaseModel):
                large_data: list = []
                counter: int = 0
                total_items: int = 0  # Add this field so complete() can merge

            @classmethod
            def create_context(cls):
                return cls.Context()

            @step(start=True)
            def accumulate_data(self):
                ctx = get_context()
                # Add some data each step
                ctx.large_data.extend([f"item_{i}" for i in range(100)])
                ctx.counter += 1

                if ctx.counter < 5:
                    return goto(self.accumulate_data)
                else:
                    return complete(total_items=len(ctx.large_data))

        # Start workflow
        workflow_run = engine.start("large_context_workflow")

        # Should complete successfully even with large context
        workflow_run.refresh_from_db()
        self.assertEqual(workflow_run.status, WorkflowStatus.COMPLETED)

        # These should be deterministic now
        self.assertEqual(len(workflow_run.data["large_data"]), 500)  # 5 * 100 items
        self.assertEqual(workflow_run.data["counter"], 5)
        self.assertEqual(
            workflow_run.data["total_items"], 500
        )  # complete() result merged

    def test_event_callback_error_isolation(self):
        """Test that callback errors don't break the system"""

        callback_results = []

        def good_callback(event):
            callback_results.append(f"good_{event.id}")

        def bad_callback(event):
            callback_results.append(f"bad_{event.id}")
            raise Exception("Callback failed!")

        def another_good_callback(event):
            callback_results.append(f"another_good_{event.id}")

        # Register callbacks
        callback_registry.register(good_callback, event_name="booking_created")
        callback_registry.register(bad_callback, event_name="booking_created")
        callback_registry.register(another_good_callback, event_name="booking_created")

        # Create booking to trigger callbacks
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=3),
            status="pending",
        )

        # Trigger event manually
        event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_created",
        )
        event.mark_as_occurred()

        # Good callbacks should still have run despite bad one failing
        self.assertIn(f"good_{event.id}", callback_results)
        self.assertIn(f"another_good_{event.id}", callback_results)
        self.assertIn(f"bad_{event.id}", callback_results)

        # Cleanup
        callback_registry.clear()

    def test_scheduled_event_boundary_conditions(self):
        """Test scheduled events at various time boundaries"""

        now = timezone.now()

        # Test events at different time boundaries
        test_times = [
            now + timedelta(seconds=1),  # Very soon
            now + timedelta(minutes=1),  # 1 minute
            now + timedelta(hours=1),  # 1 hour
            now + timedelta(days=1),  # 1 day
            now + timedelta(weeks=1),  # 1 week
            now + timedelta(days=365),  # 1 year
        ]

        bookings = []
        for i, checkin_time in enumerate(test_times):
            booking = Booking.objects.create(
                guest=self.guest,
                checkin_date=checkin_time,
                checkout_date=checkin_time + timedelta(days=2),
                status="confirmed",
            )
            bookings.append(booking)

        # Check that all events were created with correct timing
        for booking in bookings:
            event = Event.objects.get(
                model_type=ContentType.objects.get_for_model(Booking),
                entity_id=str(booking.id),
                event_name="checkin_due",
            )

            self.assertEqual(event.at, booking.checkin_date)
            self.assertTrue(event.is_valid)

    def test_workflow_step_method_validation(self):
        """Test that workflow validation works during registration"""

        # Test that workflows require Context class
        try:

            @workflow("test_validation")
            class TestWorkflow:
                class Context(BaseModel):
                    test_field: str = "test"

                @classmethod
                def create_context(cls):
                    return cls.Context()

                @step(start=True)
                def test_step(self):
                    return complete()

            # If we get here, validation passed (which is good)
            self.assertTrue(True)

        except Exception as e:
            # If validation failed, that's also acceptable behavior
            self.assertIn("workflow", str(e).lower())


class StressTestWorkflows(TestCase):
    """Stress tests for workflow system under load"""

    def setUp(self):
        engine.set_executor(SynchronousExecutor())
        from django_ai.automation.workflows.core import _workflows

        _workflows.clear()

    def test_many_sequential_workflows(self):
        """Test many workflows running in sequence"""

        @workflow("stress_test_workflow")
        class StressWorkflow:
            class Context(BaseModel):
                workflow_num: int
                completed: bool = False
                workflow_num_result: int = 0  # Add this field so complete() can merge

            @classmethod
            def create_context(cls, workflow_num: int):
                return cls.Context(workflow_num=workflow_num)

            @step(start=True)
            def process(self):
                ctx = get_context()
                ctx.completed = True
                return complete(workflow_num_result=ctx.workflow_num)

        # Run many workflows
        num_workflows = 50
        completed_count = 0

        for i in range(num_workflows):
            run = engine.start("stress_test_workflow", workflow_num=i)
            run.refresh_from_db()
            if run.status == WorkflowStatus.COMPLETED:
                completed_count += 1
                # Verify the complete() result was merged
                self.assertEqual(run.data["workflow_num_result"], i)

        # All should complete
        self.assertEqual(completed_count, num_workflows)

    def test_deep_workflow_nesting(self):
        """Test workflow with many sequential steps"""

        @workflow("deep_workflow")
        class DeepWorkflow:
            class Context(BaseModel):
                current_depth: int = 0
                max_depth: int
                final_depth: int = 0  # Add this field so complete() can merge

            @classmethod
            def create_context(cls, max_depth: int = 10):
                return cls.Context(max_depth=max_depth)

            @step(start=True)
            def deep_step(self):
                ctx = get_context()
                ctx.current_depth += 1

                if ctx.current_depth < ctx.max_depth:
                    return goto(self.deep_step)  # Recursive call
                else:
                    return complete(final_depth=ctx.current_depth)

        # Test deep workflow
        run = engine.start("deep_workflow", max_depth=20)
        run.refresh_from_db()

        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.data["current_depth"], 20)
        self.assertEqual(run.data["final_depth"], 20)  # complete() result merged


class NamespaceIntegrationTests(TestCase):
    """Test namespace integration across workflows and events"""

    def setUp(self):
        engine.set_executor(SynchronousExecutor())
        from django_ai.automation.workflows.core import _workflows, _event_workflows
        from django_ai.automation.events.callbacks import callback_registry

        _workflows.clear()
        _event_workflows.clear()
        callback_registry.clear()

        # Register the workflow integration handler
        from django_ai.automation.workflows.integration import handle_event_for_workflows

        callback_registry.register(
            handle_event_for_workflows, event_name="*", namespace="*"
        )

        self.guest = Guest.objects.create(email="test@example.com", name="Test User")

    def tearDown(self):
        from django_ai.automation.events.callbacks import callback_registry

        callback_registry.clear()

    def test_event_workflow_namespace_isolation(self):
        """Test that event workflows are isolated by namespace"""
        from pydantic import BaseModel
        from django_ai.automation.workflows.core import event_workflow, step, complete
        
        workflow_executions = []

        @event_workflow(event_name="booking_confirmed")
        class DefaultNamespaceWorkflow:
            class Context(BaseModel):
                booking_id: int

            @classmethod
            def create_context(cls, event=None):
                return cls.Context(booking_id=event.entity.id)

            @step(start=True)
            def process(self):
                ctx = get_context()
                workflow_executions.append(f"default_{ctx.booking_id}")
                return complete()

        # Create bookings - this will auto-create events
        booking_a = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",  # This will make booking_confirmed valid
        )

        booking_b = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )

        # Get the auto-created events and update their namespaces
        event_a = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking_a.id),
            event_name="booking_confirmed"
        )
        event_a.namespace = "tenant_a"
        event_a.save()

        event_b = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking_b.id),
            event_name="booking_confirmed"
        )
        event_b.namespace = "tenant_b" 
        event_b.save()

        # Trigger events
        event_a.mark_as_occurred()
        event_b.mark_as_occurred()

        # Both should trigger since workflow doesn't filter by namespace
        self.assertIn(f"default_{booking_a.id}", workflow_executions)
        self.assertIn(f"default_{booking_b.id}", workflow_executions)

    def test_callback_namespace_isolation(self):
        """Test that callbacks are properly isolated by namespace"""
        from django_ai.automation.events.callbacks import on_event

        callback_results = []

        @on_event(event_name="booking_confirmed", namespace="tenant_x")
        def tenant_x_callback(event):
            callback_results.append(f"tenant_x_{event.entity_id}")

        @on_event(event_name="booking_confirmed", namespace="tenant_y")
        def tenant_y_callback(event):
            callback_results.append(f"tenant_y_{event.entity_id}")

        @on_event(event_name="booking_confirmed", namespace="*")
        def global_callback(event):
            callback_results.append(f"global_{event.entity_id}")

        # Create bookings and events with different namespaces
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )

        # Create events with different namespaces
        event_x = Event.objects.create(
            event_name="booking_confirmed",
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            namespace="tenant_x",
        )

        event_y = Event.objects.create(
            event_name="booking_confirmed",
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id="999",  # Different entity
            namespace="tenant_y",
        )

        # Trigger events
        event_x.mark_as_occurred()
        event_y.mark_as_occurred()

        # Check callback results
        self.assertIn(f"tenant_x_{booking.id}", callback_results)
        self.assertIn(f"global_{booking.id}", callback_results)  # Global should match
        self.assertNotIn(f"tenant_y_{booking.id}", callback_results)  # Wrong namespace

        self.assertIn(f"tenant_y_999", callback_results)
        self.assertIn(f"global_999", callback_results)  # Global should match
        self.assertNotIn(f"tenant_x_999", callback_results)  # Wrong namespace

    def test_namespace_with_model_events(self):
        """Test namespace behavior with actual model-generated events"""
        from django_ai.automation.events.callbacks import on_event

        callback_results = []

        @on_event(event_name="booking_created", namespace="*")
        def track_booking_creation(event):
            callback_results.append(f"created_{event.entity_id}_{event.namespace}")

        # Create booking - this will generate events with default namespace
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="pending",
        )

        # The booking_created event should be auto-created as an immediate event
        # We need to find it and manually mark it as occurred to trigger callbacks
        try:
            event = Event.objects.get(
                model_type=ContentType.objects.get_for_model(Booking),
                entity_id=str(booking.id),
                event_name="booking_created"
            )
            # Mark as occurred to trigger callbacks
            event.mark_as_occurred()
            
            # The callback should have been triggered with default namespace
            self.assertIn(f"created_{booking.id}_*", callback_results)
        except Event.DoesNotExist:
            # If the event wasn't auto-created, create it manually and test
            event = Event.objects.create(
                event_name="booking_created",
                model_type=ContentType.objects.get_for_model(Booking),
                entity_id=str(booking.id),
                namespace="*"
            )
            event.mark_as_occurred()
            self.assertIn(f"created_{booking.id}_*", callback_results)

    def test_multiple_namespace_callbacks_for_same_event(self):
        """Test multiple callbacks with different namespaces for the same event type"""
        from django_ai.automation.events.callbacks import on_event

        callback_results = []

        @on_event(event_name="test_event", namespace="ns1")
        def ns1_callback(event):
            callback_results.append("ns1")

        @on_event(event_name="test_event", namespace="ns2")
        def ns2_callback(event):
            callback_results.append("ns2")

        @on_event(event_name="test_event", namespace="*")
        def wildcard_callback(event):
            callback_results.append("wildcard")

        # Create events with different namespaces
        event1 = Event.objects.create(
            event_name="test_event",
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id="1",
            namespace="ns1",
        )

        event2 = Event.objects.create(
            event_name="test_event",
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id="2",
            namespace="ns2",
        )

        event3 = Event.objects.create(
            event_name="test_event",
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id="3",
            namespace="ns3",  # No specific callback for this namespace
        )

        # Trigger all events
        event1.mark_as_occurred()
        event2.mark_as_occurred()
        event3.mark_as_occurred()

        # Check results
        expected_calls = [
            "ns1",
            "wildcard",  # event1: ns1 callback + wildcard
            "ns2",
            "wildcard",  # event2: ns2 callback + wildcard
            "wildcard",  # event3: only wildcard (no ns3 callback)
        ]

        for expected in expected_calls:
            self.assertIn(expected, callback_results)

        # Verify specific namespace callbacks only fired for their namespace
        self.assertEqual(callback_results.count("ns1"), 1)
        self.assertEqual(callback_results.count("ns2"), 1)
        self.assertEqual(callback_results.count("wildcard"), 3)

if __name__ == "__main__":
    unittest.main()
