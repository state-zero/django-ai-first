import unittest
from datetime import timedelta
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from pydantic import BaseModel

from django_ai.automation.events.models import Event, EventStatus
from django_ai.automation.events.callbacks import callback_registry
from django_ai.automation.workflows.core import Retry, engine
from django_ai.automation.queues.sync_executor import SynchronousExecutor

from django_ai.automation.agents.core import (
    agent,
    handler,
    agent_engine,
    AgentManager,
    _agents,
    _agent_handlers,
    HandlerInfo,
)
from django_ai.automation.agents.models import (
    AgentRun,
    AgentStatus,
    HandlerExecution,
    HandlerStatus,
)
from django_ai.automation.testing import time_machine

# Import test models
from tests.models import Guest, Booking


class DecoratorValidationTests(TestCase):
    """Test decorator validation - these are unit tests that don't need time machine."""

    def setUp(self):
        _agents.clear()
        _agent_handlers.clear()

    def tearDown(self):
        _agents.clear()
        _agent_handlers.clear()

    def test_agent_decorator_requires_context(self):
        """Test that agent decorator validates Context class"""
        with self.assertRaises(ValueError) as cm:

            @agent("test_agent", spawn_on="booking_created")
            class BadAgent:
                def get_namespace(self, event):
                    return "test"

                @classmethod
                def create_context(cls, event):
                    return {}

        self.assertIn("Context", str(cm.exception))

    def test_agent_decorator_requires_get_namespace(self):
        """Test that agent decorator validates get_namespace method"""
        with self.assertRaises(ValueError) as cm:

            @agent("test_agent", spawn_on="booking_created")
            class BadAgent:
                class Context(BaseModel):
                    pass

                @classmethod
                def create_context(cls, event):
                    return cls.Context()

        self.assertIn("get_namespace", str(cm.exception))

    def test_agent_decorator_requires_create_context(self):
        """Test that agent decorator validates create_context method"""
        with self.assertRaises(ValueError) as cm:

            @agent("test_agent", spawn_on="booking_created")
            class BadAgent:
                class Context(BaseModel):
                    pass

                def get_namespace(self, event):
                    return "test"

        self.assertIn("create_context", str(cm.exception))

    def test_handler_decorator_stores_metadata(self):
        """Test that handler decorator stores correct metadata"""

        def my_condition(event, ctx):
            return True

        retry_policy = Retry(max_attempts=5)

        @handler(
            "move_in",
            offset_minutes=-180,
            condition=my_condition,
            retry=retry_policy,
        )
        def test_handler(self, ctx):
            pass

        self.assertTrue(test_handler._is_handler)
        self.assertEqual(test_handler._handler_event_name, "move_in")
        self.assertEqual(test_handler._handler_offset_minutes, -180)
        self.assertEqual(test_handler._handler_condition, my_condition)
        self.assertEqual(test_handler._handler_retry, retry_policy)

    def test_agent_registers_globally(self):
        """Test that agent decorator registers agent globally"""

        @agent("my_agent", spawn_on="booking_created")
        class MyAgent:
            class Context(BaseModel):
                value: int = 0

            def get_namespace(self, event):
                return "test"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

        self.assertIn("my_agent", _agents)
        self.assertEqual(_agents["my_agent"], MyAgent)

    def test_agent_collects_handlers(self):
        """Test that agent decorator collects all handler methods"""

        @agent("collector_agent", spawn_on="booking_created")
        class CollectorAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return "test"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

            @handler("move_in", offset_minutes=-180)
            def handler_one(self, ctx):
                pass

            @handler("move_in", offset_minutes=0)
            def handler_two(self, ctx):
                pass

            @handler("move_out")
            def handler_three(self, ctx):
                pass

        handlers = _agent_handlers["collector_agent"]
        self.assertEqual(len(handlers), 3)

        handler_names = [h.method_name for h in handlers]
        self.assertIn("handler_one", handler_names)
        self.assertIn("handler_two", handler_names)
        self.assertIn("handler_three", handler_names)


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class AgentIntegrationTests(TransactionTestCase):
    """Integration tests using time machine for realistic time-based testing."""

    def setUp(self):
        # Use SynchronousExecutor for testing
        engine.set_executor(SynchronousExecutor())
        agent_engine.set_executor(SynchronousExecutor())
        _agents.clear()
        _agent_handlers.clear()
        callback_registry.clear()

        # Register the workflow integration handler
        from django_ai.automation.workflows.integration import handle_event_for_workflows

        callback_registry.register(
            handle_event_for_workflows, event_name="*", namespace="*"
        )

        self.guest = Guest.objects.create(email="test@example.com", name="Test User")

    def tearDown(self):
        callback_registry.clear()
        _agents.clear()
        _agent_handlers.clear()

    def test_agent_spawn_via_immediate_event(self):
        """Agent spawns when spawn_on event occurs."""

        @agent("spawn_test_agent", spawn_on="booking_created")
        class SpawnTestAgent:
            class Context(BaseModel):
                booking_id: int = 0

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context(booking_id=event.entity.id)

        with time_machine() as tm:
            # Create booking - this fires booking_created immediately
            booking = Booking.objects.create(
                guest=self.guest,
                checkin_date=timezone.now() + timedelta(days=1),
                checkout_date=timezone.now() + timedelta(days=2),
                status="pending",
            )

            # Agent should have been created
            agent_run = AgentRun.objects.filter(
                agent_name="spawn_test_agent",
                namespace=f"booking:{booking.id}",
            ).first()

            self.assertIsNotNone(agent_run)
            self.assertEqual(agent_run.status, AgentStatus.ACTIVE)
            self.assertEqual(agent_run.context["booking_id"], booking.id)

    def test_singleton_prevents_duplicate_spawn(self):
        """singleton=True prevents duplicate agents for same namespace."""

        @agent("singleton_agent", spawn_on="booking_created", singleton=True)
        class SingletonAgent:
            class Context(BaseModel):
                spawn_count: int = 0

            def get_namespace(self, event):
                return "shared_namespace"

            @classmethod
            def create_context(cls, event):
                return cls.Context(spawn_count=1)

        with time_machine() as tm:
            # Create first booking
            Booking.objects.create(
                guest=self.guest,
                checkin_date=timezone.now() + timedelta(days=1),
                checkout_date=timezone.now() + timedelta(days=2),
                status="pending",
            )

            # Create second booking (should not create new agent)
            Booking.objects.create(
                guest=self.guest,
                checkin_date=timezone.now() + timedelta(days=3),
                checkout_date=timezone.now() + timedelta(days=4),
                status="pending",
            )

            # Should only have one active agent
            active_agents = AgentRun.objects.filter(
                agent_name="singleton_agent",
                namespace="shared_namespace",
                status=AgentStatus.ACTIVE,
            )
            self.assertEqual(active_agents.count(), 1)

    def test_handler_executes_on_event(self):
        """Handler executes when its event fires."""
        handler_log = []

        @agent("handler_exec_agent", spawn_on="booking_created")
        class HandlerExecAgent:
            class Context(BaseModel):
                handled: bool = False

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

            @handler("booking_confirmed")
            def on_confirmed(self, ctx):
                handler_log.append("on_confirmed_called")
                ctx.handled = True

        with time_machine() as tm:
            # Create booking - spawns agent
            booking = Booking.objects.create(
                guest=self.guest,
                checkin_date=timezone.now() + timedelta(days=1),
                checkout_date=timezone.now() + timedelta(days=2),
                status="pending",
            )

            agent_run = AgentRun.objects.get(
                agent_name="handler_exec_agent",
                namespace=f"booking:{booking.id}",
            )

            # Update booking status - triggers booking_confirmed event
            booking.status = "confirmed"
            booking.save()

            # Handler should have been called
            self.assertIn("on_confirmed_called", handler_log)

            # Context should have been updated
            agent_run.refresh_from_db()
            self.assertTrue(agent_run.context["handled"])

    def test_handler_with_positive_offset_executes_after_event_time(self):
        """Handler with positive offset executes after the event's scheduled time."""
        handler_log = []

        @agent("offset_agent", spawn_on="booking_created")
        class OffsetAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

            @handler("move_in", offset_minutes=60)  # 1 hour after checkin
            def send_followup(self, ctx):
                handler_log.append("followup_sent")

        with time_machine() as tm:
            # Checkin is 2 hours from now
            checkin_time = timezone.now() + timedelta(hours=2)

            booking = Booking.objects.create(
                guest=self.guest,
                checkin_date=checkin_time,
                checkout_date=checkin_time + timedelta(days=1),
                status="confirmed",
            )

            # Agent spawned
            self.assertEqual(
                AgentRun.objects.filter(agent_name="offset_agent").count(), 1
            )

            # Advance to checkin time - handler should NOT fire yet
            tm.advance(hours=2)
            self.assertNotIn("followup_sent", handler_log)

            # Advance 30 more minutes - still not yet (need 60 min offset)
            tm.advance(minutes=30)
            self.assertNotIn("followup_sent", handler_log)

            # Advance 31 more minutes (now 61 min past checkin) - should fire
            tm.advance(minutes=31)
            self.assertIn("followup_sent", handler_log)

    def test_handler_with_negative_offset_executes_when_event_fires(self):
        """
        Handler with negative offset executes immediately when the event fires,
        since its scheduled_at will be in the past relative to the event's at time.

        Note: Currently, handlers are only scheduled when the event fires (at event.at),
        not proactively. So a -180 minute offset handler fires immediately when the
        move_in event fires (at checkin time), not 3 hours before.
        """
        handler_log = []

        @agent("pre_checkin_agent", spawn_on="booking_created")
        class PreCheckinAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

            @handler("move_in", offset_minutes=-180)  # 3 hours before checkin
            def send_pre_checkin(self, ctx):
                handler_log.append("pre_checkin_sent")

        with time_machine() as tm:
            # Checkin is 2 hours from now
            checkin_time = timezone.now() + timedelta(hours=2)

            booking = Booking.objects.create(
                guest=self.guest,
                checkin_date=checkin_time,
                checkout_date=checkin_time + timedelta(days=1),
                status="confirmed",
            )

            # Handler not yet fired - move_in event hasn't occurred
            self.assertNotIn("pre_checkin_sent", handler_log)

            # Advance to just past checkin - move_in event fires, handler runs immediately
            # (since scheduled_at = checkin - 180min is in the past)
            tm.advance(hours=2, minutes=1)
            self.assertIn("pre_checkin_sent", handler_log)

    def test_multiple_handlers_different_offsets_fire_in_order(self):
        """
        Multiple handlers for same event with different offsets fire at correct times.

        Note: Handlers are only scheduled when the event fires. So:
        - Negative offset handlers fire immediately (scheduled_at is in the past)
        - Zero offset handlers fire immediately
        - Positive offset handlers fire after their delay
        """
        handler_log = []

        @agent("multi_offset_agent", spawn_on="booking_created")
        class MultiOffsetAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

            @handler("move_in", offset_minutes=-60)  # Would be 1 hour before, fires immediately
            def one_hour_before(self, ctx):
                handler_log.append("1h_before")

            @handler("move_in", offset_minutes=0)  # At checkin
            def at_checkin(self, ctx):
                handler_log.append("at_checkin")

            @handler("move_in", offset_minutes=60)  # 1 hour after
            def one_hour_after(self, ctx):
                handler_log.append("1h_after")

        with time_machine() as tm:
            # Checkin is 2 hours from now
            checkin_time = timezone.now() + timedelta(hours=2)

            Booking.objects.create(
                guest=self.guest,
                checkin_date=checkin_time,
                checkout_date=checkin_time + timedelta(days=1),
                status="confirmed",
            )

            # Before checkin - no handlers fire yet
            tm.advance(hours=1)
            self.assertNotIn("1h_before", handler_log)
            self.assertNotIn("at_checkin", handler_log)
            self.assertNotIn("1h_after", handler_log)

            # Advance to checkin time - event fires
            # Negative offset and zero offset handlers fire immediately
            tm.advance(hours=1, minutes=1)
            self.assertIn("1h_before", handler_log)  # Fired immediately (was in past)
            self.assertIn("at_checkin", handler_log)  # Fired immediately
            self.assertNotIn("1h_after", handler_log)  # Not yet - needs 1 more hour

            # Advance 1 hour past checkin - positive offset handler fires
            tm.advance(hours=1)
            self.assertIn("1h_after", handler_log)

    def test_handler_condition_prevents_execution(self):
        """condition=False prevents handler execution."""
        handler_log = []

        def always_false(event, ctx):
            return False

        @agent("condition_test_agent", spawn_on="booking_created")
        class ConditionTestAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

            @handler("booking_confirmed", condition=always_false)
            def conditional_handler(self, ctx):
                handler_log.append("should_not_run")

        with time_machine() as tm:
            booking = Booking.objects.create(
                guest=self.guest,
                checkin_date=timezone.now() + timedelta(days=1),
                checkout_date=timezone.now() + timedelta(days=2),
                status="confirmed",
            )

            # Handler should have been skipped
            self.assertNotIn("should_not_run", handler_log)

            # Verify execution was marked as skipped
            execution = HandlerExecution.objects.filter(
                agent_run__agent_name="condition_test_agent",
                handler_name="conditional_handler",
            ).first()
            self.assertEqual(execution.status, HandlerStatus.SKIPPED)

    def test_handler_updates_context(self):
        """Handler can update agent context."""

        @agent("context_update_agent", spawn_on="booking_created")
        class ContextUpdateAgent:
            class Context(BaseModel):
                counter: int = 0
                messages: list = []

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

            @handler("booking_confirmed")
            def increment_and_log(self, ctx):
                ctx.counter += 1
                ctx.messages.append("confirmed")

        with time_machine() as tm:
            booking = Booking.objects.create(
                guest=self.guest,
                checkin_date=timezone.now() + timedelta(days=1),
                checkout_date=timezone.now() + timedelta(days=2),
                status="confirmed",
            )

            # Verify context was updated
            agent_run = AgentRun.objects.get(
                agent_name="context_update_agent",
                namespace=f"booking:{booking.id}",
            )
            self.assertEqual(agent_run.context["counter"], 1)
            self.assertEqual(agent_run.context["messages"], ["confirmed"])

    def test_complete_reservation_journey(self):
        """
        Test a realistic reservation journey with multiple timed handlers.

        Note: Handlers are scheduled when events fire, so negative offset handlers
        fire immediately when the event fires (not before).
        """
        journey_log = []

        @agent("reservation_journey", spawn_on="booking_created")
        class ReservationJourney:
            class Context(BaseModel):
                reservation_id: int = 0
                guest_name: str = ""
                pre_checkin_sent: bool = False
                welcome_sent: bool = False
                checkout_sent: bool = False

            def get_namespace(self, event):
                return f"reservation:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                booking = event.entity
                return cls.Context(
                    reservation_id=booking.id,
                    guest_name=booking.guest.name,
                )

            @handler("booking_confirmed", offset_minutes=0)
            def send_confirmation(self, ctx):
                journey_log.append(f"confirmation:{ctx.reservation_id}")

            @handler("move_in", offset_minutes=-180)  # Fires immediately when move_in event fires
            def send_pre_checkin(self, ctx):
                journey_log.append(f"pre_checkin:{ctx.reservation_id}")
                ctx.pre_checkin_sent = True

            @handler("move_in", offset_minutes=0)
            def send_welcome(self, ctx):
                journey_log.append(f"welcome:{ctx.reservation_id}")
                ctx.welcome_sent = True

            @handler("move_out", offset_minutes=0)
            def send_checkout(self, ctx):
                journey_log.append(f"checkout:{ctx.reservation_id}")
                ctx.checkout_sent = True

        with time_machine() as tm:
            # Checkin is 2 hours from now, checkout 2 days later
            checkin = timezone.now() + timedelta(hours=2)
            checkout = checkin + timedelta(days=2)

            booking = Booking.objects.create(
                guest=self.guest,
                checkin_date=checkin,
                checkout_date=checkout,
                status="confirmed",
            )

            # Confirmation should have been sent immediately (booking_confirmed is immediate)
            self.assertIn(f"confirmation:{booking.id}", journey_log)

            # Before checkin - move_in handlers haven't fired yet
            tm.advance(hours=1)
            self.assertNotIn(f"pre_checkin:{booking.id}", journey_log)
            self.assertNotIn(f"welcome:{booking.id}", journey_log)

            # Advance to checkin time - move_in event fires
            # Both pre_checkin (-180 offset) and welcome (0 offset) fire immediately
            tm.advance(hours=1, minutes=1)
            self.assertIn(f"pre_checkin:{booking.id}", journey_log)
            self.assertIn(f"welcome:{booking.id}", journey_log)

            # Advance to checkout time - move_out event fires
            tm.advance(days=2)
            self.assertIn(f"checkout:{booking.id}", journey_log)

            # Verify all context flags were set
            agent_run = AgentRun.objects.get(
                agent_name="reservation_journey",
                namespace=f"reservation:{booking.id}",
            )
            self.assertTrue(agent_run.context["pre_checkin_sent"])
            self.assertTrue(agent_run.context["welcome_sent"])
            self.assertTrue(agent_run.context["checkout_sent"])


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class AgentManagerTests(TransactionTestCase):
    """Test AgentManager utility methods."""

    def setUp(self):
        agent_engine.set_executor(SynchronousExecutor())
        _agents.clear()
        _agent_handlers.clear()
        callback_registry.clear()

    def tearDown(self):
        _agents.clear()
        _agent_handlers.clear()

    def test_agent_manager_manual_spawn(self):
        """AgentManager.spawn creates agent manually."""

        @agent("manual_agent", spawn_on="booking_created")
        class ManualAgent:
            class Context(BaseModel):
                custom_value: str = ""

            def get_namespace(self, event):
                return "test"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

        agent_run = AgentManager.spawn(
            "manual_agent",
            namespace="custom_namespace",
            custom_value="hello",
        )

        self.assertIsNotNone(agent_run)
        self.assertEqual(agent_run.agent_name, "manual_agent")
        self.assertEqual(agent_run.namespace, "custom_namespace")
        self.assertEqual(agent_run.context["custom_value"], "hello")
        self.assertEqual(agent_run.status, AgentStatus.ACTIVE)

    def test_agent_manager_complete(self):
        """AgentManager.complete marks agent as completed."""

        @agent("complete_test_agent", spawn_on="booking_created")
        class CompleteTestAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return "test"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

        agent_run = AgentManager.spawn("complete_test_agent", namespace="test")
        self.assertEqual(agent_run.status, AgentStatus.ACTIVE)

        AgentManager.complete(agent_run)

        agent_run.refresh_from_db()
        self.assertEqual(agent_run.status, AgentStatus.COMPLETED)

    def test_agent_manager_list_active(self):
        """AgentManager.list_active returns correct agents."""

        @agent("list_test_agent", spawn_on="booking_created", singleton=False)
        class ListTestAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return "test"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

        # Create multiple agents
        agent1 = AgentManager.spawn("list_test_agent", namespace="ns1")
        agent2 = AgentManager.spawn("list_test_agent", namespace="ns2")
        agent3 = AgentManager.spawn("list_test_agent", namespace="ns3")

        # Complete one
        AgentManager.complete(agent2)

        # List all active
        active = AgentManager.list_active("list_test_agent")
        self.assertEqual(len(active), 2)

        # List by namespace
        active_ns1 = AgentManager.list_active("list_test_agent", namespace="ns1")
        self.assertEqual(len(active_ns1), 1)
        self.assertEqual(active_ns1[0].namespace, "ns1")


if __name__ == "__main__":
    unittest.main()
