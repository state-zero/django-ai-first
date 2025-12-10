import unittest
from datetime import timedelta
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from pydantic import BaseModel

from django_ai.automation.events.models import Event, EventStatus
from django_ai.automation.events.callbacks import callback_registry
from django_ai.automation.workflows.core import Retry

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

# Import test models
from tests.models import Guest, Booking


class MockExecutor:
    """Simple mock executor for testing"""

    def __init__(self):
        self.queued_tasks = []

    def queue_task(self, task_name, *args, delay=None):
        self.queued_tasks.append({"task_name": task_name, "args": args, "delay": delay})

        # Execute handler immediately for testing
        if task_name == "execute_handler":
            execution_id = args[0]
            agent_engine.execute_handler(execution_id)

    def clear(self):
        self.queued_tasks.clear()


@override_settings(SKIP_Q2_AUTOSCHEDULE=True)
class HandlerBasedAgentTests(TransactionTestCase):
    """Test suite for handler-based agent functionality"""

    def setUp(self):
        """Set up test environment"""
        agent_engine.set_executor(MockExecutor())
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

    # ============================================================================
    # DECORATOR VALIDATION TESTS
    # ============================================================================

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

    # ============================================================================
    # AGENT REGISTRATION TESTS
    # ============================================================================

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

    # ============================================================================
    # SPAWN TESTS
    # ============================================================================

    def test_agent_spawn_via_event(self):
        """Test agent spawning when spawn_on event occurs"""

        @agent("spawn_test_agent", spawn_on="booking_created")
        class SpawnTestAgent:
            class Context(BaseModel):
                booking_id: int = 0

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context(booking_id=event.entity.id)

        # Create booking (triggers booking_created event)
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="pending",
        )

        # Get and trigger the event
        event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_created",
        )
        event.mark_as_occurred()

        # Verify agent was created
        agent_run = AgentRun.objects.filter(
            agent_name="spawn_test_agent",
            namespace=f"booking:{booking.id}",
        ).first()

        self.assertIsNotNone(agent_run)
        self.assertEqual(agent_run.status, AgentStatus.ACTIVE)
        self.assertEqual(agent_run.context["booking_id"], booking.id)

    def test_singleton_prevents_duplicate_spawn(self):
        """Test singleton=True prevents duplicate agents for same namespace"""

        @agent("singleton_agent", spawn_on="booking_created", singleton=True)
        class SingletonAgent:
            class Context(BaseModel):
                spawn_count: int = 0

            def get_namespace(self, event):
                return "shared_namespace"

            @classmethod
            def create_context(cls, event):
                return cls.Context(spawn_count=1)

        # Create first booking
        booking1 = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="pending",
        )

        event1 = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking1.id),
            event_name="booking_created",
        )
        event1.mark_as_occurred()

        # Create second booking (should not create new agent)
        booking2 = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=3),
            checkout_date=timezone.now() + timedelta(days=4),
            status="pending",
        )

        event2 = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking2.id),
            event_name="booking_created",
        )
        event2.mark_as_occurred()

        # Should only have one active agent
        active_agents = AgentRun.objects.filter(
            agent_name="singleton_agent",
            namespace="shared_namespace",
            status=AgentStatus.ACTIVE,
        )
        self.assertEqual(active_agents.count(), 1)

    def test_non_singleton_allows_multiple_agents(self):
        """Test singleton=False allows multiple agents per namespace"""

        @agent("non_singleton_agent", spawn_on="booking_created", singleton=False)
        class NonSingletonAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return "shared_namespace"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

        # Create multiple bookings
        for i in range(3):
            booking = Booking.objects.create(
                guest=self.guest,
                checkin_date=timezone.now() + timedelta(days=i + 1),
                checkout_date=timezone.now() + timedelta(days=i + 2),
                status="pending",
            )

            event = Event.objects.get(
                model_type=ContentType.objects.get_for_model(Booking),
                entity_id=str(booking.id),
                event_name="booking_created",
            )
            event.mark_as_occurred()

        # Should have 3 agents
        active_agents = AgentRun.objects.filter(
            agent_name="non_singleton_agent",
            status=AgentStatus.ACTIVE,
        )
        self.assertEqual(active_agents.count(), 3)

    # ============================================================================
    # HANDLER EXECUTION TESTS
    # ============================================================================

    def test_handler_executes_when_agent_exists(self):
        """Test handler executes when agent exists for namespace"""
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

        # Create booking and spawn agent
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="pending",
        )

        created_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_created",
        )
        created_event.mark_as_occurred()

        # Verify agent exists
        agent_run = AgentRun.objects.get(
            agent_name="handler_exec_agent",
            namespace=f"booking:{booking.id}",
        )

        # Update booking status to trigger confirmed event
        booking.status = "confirmed"
        booking.save()

        confirmed_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_confirmed",
        )
        confirmed_event.mark_as_occurred()

        # Verify handler was called
        self.assertIn("on_confirmed_called", handler_log)

        # Verify context was updated
        agent_run.refresh_from_db()
        self.assertTrue(agent_run.context["handled"])

    def test_handler_skipped_when_no_agent(self):
        """Test handler is skipped when no agent exists for namespace"""
        handler_log = []

        # Use a spawn event that doesn't exist for this booking
        @agent("skip_test_agent", spawn_on="some_other_event")
        class SkipTestAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

            @handler("booking_confirmed")
            def on_confirmed(self, ctx):
                handler_log.append("should_not_be_called")

        # Create booking - this does NOT spawn the agent because spawn_on is "some_other_event"
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )

        # Trigger the confirmed event - no agent exists, so handler should be skipped
        confirmed_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_confirmed",
        )
        confirmed_event.mark_as_occurred()

        # Handler should not have been called because no agent was spawned
        self.assertNotIn("should_not_be_called", handler_log)

        # Verify no executions were created (handler was completely skipped)
        executions = HandlerExecution.objects.filter(
            handler_name="on_confirmed",
        )
        self.assertEqual(executions.count(), 0)

    def test_multiple_handlers_same_event_different_offsets(self):
        """Test multiple handlers for same event with different offsets"""
        handler_log = []

        @agent("multi_handler_agent", spawn_on="booking_created")
        class MultiHandlerAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

            @handler("booking_confirmed", offset_minutes=0)
            def on_confirmed_immediate(self, ctx):
                handler_log.append("immediate")

            @handler("booking_confirmed", offset_minutes=-60)
            def on_confirmed_before(self, ctx):
                handler_log.append("before")

            @handler("booking_confirmed", offset_minutes=60)
            def on_confirmed_after(self, ctx):
                handler_log.append("after")

        # Create booking and spawn agent
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="pending",
        )

        created_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_created",
        )
        created_event.mark_as_occurred()

        # Update to confirmed
        booking.status = "confirmed"
        booking.save()

        confirmed_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_confirmed",
        )
        confirmed_event.mark_as_occurred()

        # Verify all handlers created executions
        executions = HandlerExecution.objects.filter(
            agent_run__agent_name="multi_handler_agent",
            event_name="booking_confirmed",
        )
        self.assertEqual(executions.count(), 3)

    def test_handler_condition_prevents_execution(self):
        """Test that condition=False prevents handler execution"""
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

        # Create and spawn
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="pending",
        )

        created_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_created",
        )
        created_event.mark_as_occurred()

        # Update and trigger handler
        booking.status = "confirmed"
        booking.save()

        confirmed_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_confirmed",
        )
        confirmed_event.mark_as_occurred()

        # Handler should have been skipped
        self.assertNotIn("should_not_run", handler_log)

        # Verify execution was marked as skipped
        execution = HandlerExecution.objects.filter(
            agent_run__agent_name="condition_test_agent",
            handler_name="conditional_handler",
        ).first()
        self.assertEqual(execution.status, HandlerStatus.SKIPPED)

    def test_handler_condition_allows_execution(self):
        """Test that condition=True allows handler execution"""
        handler_log = []

        def always_true(event, ctx):
            return True

        @agent("condition_pass_agent", spawn_on="booking_created")
        class ConditionPassAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

            @handler("booking_confirmed", condition=always_true)
            def conditional_handler(self, ctx):
                handler_log.append("did_run")

        # Create and spawn
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="pending",
        )

        created_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_created",
        )
        created_event.mark_as_occurred()

        # Update and trigger handler
        booking.status = "confirmed"
        booking.save()

        confirmed_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_confirmed",
        )
        confirmed_event.mark_as_occurred()

        # Handler should have run
        self.assertIn("did_run", handler_log)

    # ============================================================================
    # CONTEXT UPDATE TESTS
    # ============================================================================

    def test_handler_updates_context(self):
        """Test that handler can update agent context"""

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

        # Create and spawn
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="pending",
        )

        created_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_created",
        )
        created_event.mark_as_occurred()

        # Update and trigger handler
        booking.status = "confirmed"
        booking.save()

        confirmed_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_confirmed",
        )
        confirmed_event.mark_as_occurred()

        # Verify context was updated
        agent_run = AgentRun.objects.get(
            agent_name="context_update_agent",
            namespace=f"booking:{booking.id}",
        )
        self.assertEqual(agent_run.context["counter"], 1)
        self.assertEqual(agent_run.context["messages"], ["confirmed"])

    # ============================================================================
    # RETRY TESTS
    # ============================================================================

    def test_handler_retry_on_error(self):
        """Test that handler retries on error when retry policy is set"""
        attempt_count = [0]  # Use list to allow modification in nested function

        @agent("retry_test_agent", spawn_on="booking_created")
        class RetryTestAgent:
            class Context(BaseModel):
                pass

            def get_namespace(self, event):
                return f"booking:{event.entity.id}"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

            @handler(
                "booking_confirmed",
                retry=Retry(max_attempts=3, base_delay=timedelta(seconds=0)),
            )
            def flaky_handler(self, ctx):
                attempt_count[0] += 1
                if attempt_count[0] < 2:
                    raise Exception("Simulated failure")

        # Create and spawn
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="pending",
        )

        created_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_created",
        )
        created_event.mark_as_occurred()

        # Update and trigger handler
        booking.status = "confirmed"
        booking.save()

        confirmed_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_confirmed",
        )
        confirmed_event.mark_as_occurred()

        # Handler should have been called twice (first failed, second succeeded)
        self.assertEqual(attempt_count[0], 2)

        # Verify execution completed
        execution = HandlerExecution.objects.filter(
            agent_run__agent_name="retry_test_agent",
            handler_name="flaky_handler",
        ).first()
        self.assertEqual(execution.status, HandlerStatus.COMPLETED)
        self.assertEqual(execution.attempt_count, 2)

    # ============================================================================
    # AGENT MANAGER TESTS
    # ============================================================================

    def test_agent_manager_manual_spawn(self):
        """Test AgentManager.spawn for manual agent creation"""

        @agent("manual_agent", spawn_on="booking_created")
        class ManualAgent:
            class Context(BaseModel):
                custom_value: str = ""

            def get_namespace(self, event):
                return "test"

            @classmethod
            def create_context(cls, event):
                return cls.Context()

        # Manual spawn with custom context
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
        """Test AgentManager.complete marks agent as completed"""

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
        """Test AgentManager.list_active returns correct agents"""

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

    # ============================================================================
    # REALISTIC USE CASE TEST
    # ============================================================================

    def test_reservation_journey_agent(self):
        """Test realistic reservation journey agent with multiple handlers"""
        journey_log = []

        @agent("reservation_journey", spawn_on="booking_created")
        class ReservationJourney:
            class Context(BaseModel):
                reservation_id: int = 0
                guest_name: str = ""
                pre_checkin_sent: bool = False
                access_code_created: bool = False
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
                journey_log.append(f"confirmation_sent:{ctx.reservation_id}")

            @handler("move_in", offset_minutes=-180)  # 3 hours before
            def send_pre_checkin(self, ctx):
                journey_log.append(f"pre_checkin:{ctx.reservation_id}")
                ctx.pre_checkin_sent = True

            @handler("move_in", offset_minutes=-60)  # 1 hour before
            def create_access_code(self, ctx):
                journey_log.append(f"access_code:{ctx.reservation_id}")
                ctx.access_code_created = True

            @handler("move_in", offset_minutes=0)
            def send_welcome(self, ctx):
                journey_log.append(f"welcome:{ctx.reservation_id}")
                ctx.welcome_sent = True

            @handler("move_out", offset_minutes=0)
            def send_checkout(self, ctx):
                journey_log.append(f"checkout:{ctx.reservation_id}")
                ctx.checkout_sent = True

        # Create booking
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=3),
            status="pending",
        )

        # Trigger spawn event
        created_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_created",
        )
        created_event.mark_as_occurred()

        # Verify agent was created
        agent_run = AgentRun.objects.get(
            agent_name="reservation_journey",
            namespace=f"reservation:{booking.id}",
        )
        self.assertEqual(agent_run.context["reservation_id"], booking.id)
        self.assertEqual(agent_run.context["guest_name"], "Test User")

        # Confirm booking
        booking.status = "confirmed"
        booking.save()

        confirmed_event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_confirmed",
        )
        confirmed_event.mark_as_occurred()

        self.assertIn(f"confirmation_sent:{booking.id}", journey_log)

        # Verify handlers were registered
        handlers = agent_engine.list_handlers("reservation_journey")
        self.assertEqual(len(handlers), 5)  # 5 handlers defined

        handler_events = [h.event_name for h in handlers]
        self.assertIn("booking_confirmed", handler_events)
        self.assertIn("move_in", handler_events)
        self.assertIn("move_out", handler_events)


if __name__ == "__main__":
    unittest.main()
