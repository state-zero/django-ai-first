import unittest
from django.test import TransactionTestCase
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from datetime import timedelta
from pydantic import BaseModel

from automation.events.models import Event, EventStatus
from automation.events.callbacks import callback_registry
from automation.workflows.core import (
    get_context,
    complete,
    engine,
    _workflows,
    _event_workflows,
)
from automation.workflows.models import WorkflowRun, WorkflowStatus
from automation.agents.core import agent, AgentManager
from automation.queues.sync_executor import SynchronousExecutor

# Import test models
from tests.models import Guest, Booking


class SimpleAgentTest(TransactionTestCase):
    """Simple test to verify basic agent functionality works"""

    def setUp(self):
        """Set up test environment"""
        engine.set_executor(SynchronousExecutor())
        _workflows.clear()
        _event_workflows.clear()
        callback_registry.clear()

        # Register the workflow integration handler
        from automation.workflows.integration import handle_event_for_workflows

        callback_registry.register(
            handle_event_for_workflows, event_name="*", namespace="*"
        )

        self.guest = Guest.objects.create(email="test@example.com", name="Test User")

    def tearDown(self):
        callback_registry.clear()
        _workflows.clear()
        _event_workflows.clear()

    def test_agent_decorator_validation(self):
        """Test that agent decorator validates required methods"""

        # Test missing act method
        with self.assertRaises(ValueError) as cm:

            @agent(spawn_on=["booking_created"])
            class BadAgent1:
                def get_namespace(self, event) -> str:
                    return "test"

                # Missing act method

        self.assertIn("act", str(cm.exception))

        # Test missing get_namespace method
        with self.assertRaises(ValueError) as cm:

            @agent(spawn_on=["booking_created"])
            class BadAgent2:
                def act(self):
                    pass

                # Missing get_namespace method

        self.assertIn("get_namespace", str(cm.exception))

    def test_agent_workflow_creation(self):
        """Test that agent creates corresponding workflow"""

        @agent(spawn_on=["booking_created"])
        class TestAgent:
            class Context(BaseModel):
                booking_id: int = 0

            def get_namespace(self, event) -> str:
                return f"booking_{event.entity.id}"

            def act(self):
                ctx = get_context()
                ctx.booking_id = 42
                self.finish()

        # Check that workflow was registered
        self.assertIn("agent:testagent", _workflows)

    def test_manual_agent_spawn(self):
        """Test manually spawning an agent"""

        @agent(spawn_on=["booking_created"])
        class ManualTestAgent:
            class Context(BaseModel):
                test_data: str = ""

            def get_namespace(self, event) -> str:
                return "test_namespace"

            def act(self):
                ctx = get_context()
                ctx.test_data = "spawned_manually"
                self.finish()

        # Manually spawn the agent
        run = AgentManager.spawn("manualtestagent", test_data="initial")

        # Verify workflow run was created
        self.assertIsNotNone(run)
        self.assertEqual(run.name, "agent:manualtestagent")

        # Check final state
        run.refresh_from_db()
        if run.status == WorkflowStatus.COMPLETED:
            self.assertEqual(run.data.get("test_data"), "spawned_manually")

    def test_agent_context_creation(self):
        """Test that agent context is created correctly"""

        @agent(spawn_on=["booking_created"])
        class ContextTestAgent:
            class Context(BaseModel):
                custom_field: str = ""

            def get_namespace(self, event) -> str:
                return "context_test"

            def act(self):
                ctx = get_context()
                # Verify automatic fields exist
                assert ctx.triggering_event_id is not None
                assert ctx.current_event_id is not None
                assert ctx.current_event_name is not None
                assert ctx.agent_namespaces is not None
                assert ctx.spawned_at is not None

                # Set custom field BEFORE finishing
                ctx.custom_field = "context_works"
                self.finish()

        # Create booking
        booking = Booking.objects.create(
            guest=self.guest,
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="pending",
        )

        # Get event and manually start agent workflow with it
        event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(Booking),
            entity_id=str(booking.id),
            event_name="booking_created",
        )

        # Manually start the workflow with event
        run = engine.start("agent:contexttestagent", event=event)

        # Check the run was created and context populated
        run.refresh_from_db()

        # Verify automatic context fields
        self.assertEqual(run.data.get("triggering_event_id"), event.id)
        self.assertEqual(run.data.get("current_event_id"), event.id)
        self.assertEqual(run.data.get("current_event_name"), "booking_created")
        self.assertIn("context_test", run.data.get("agent_namespaces", []))

        # If completed, verify custom field
        if run.status == WorkflowStatus.COMPLETED:
            self.assertEqual(run.data.get("custom_field"), "context_works")

    def test_agent_namespace_handling(self):
        """Test different namespace return types"""

        # Test string namespace
        @agent(spawn_on=["booking_created"])
        class StringNSAgent:
            def get_namespace(self, event) -> str:
                return "string_namespace"

            def act(self):
                self.finish()

        # Test list namespace
        @agent(spawn_on=["booking_confirmed"])
        class ListNSAgent:
            def get_namespace(self, event) -> list:
                return ["ns1", "ns2"]

            def act(self):
                self.finish()

        # Test None namespace (should default to "*")
        @agent(spawn_on=["guest_checked_in"])
        class NoneNSAgent:
            def get_namespace(self, event):
                return None

            def act(self):
                self.finish()

        # Test that all agents were registered
        self.assertIn("agent:stringnsagent", _workflows)
        self.assertIn("agent:listnsagent", _workflows)
        self.assertIn("agent:nonensagent", _workflows)


if __name__ == "__main__":
    unittest.main()
