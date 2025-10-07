from datetime import timedelta
from django.test import TestCase
from pydantic import BaseModel
from rest_framework import serializers

from ..core import (
    workflow,
    step,
    goto,
    complete,
    engine,
    get_context,
    wait_for_event,
)
from ..models import WorkflowRun, WorkflowStatus
from ..statezero_action import statezero_action
from ..metadata import StepDisplayMetadata, FieldGroup, FieldDisplayConfig
from django_ai.automation.queues.sync_executor import SynchronousExecutor

class TestDisplayMetadata(TestCase):
    """Test display metadata for completion, waiting, and step display"""

    def setUp(self):
        self.executor = SynchronousExecutor()
        engine.set_executor(self.executor)
        from ..core import _workflows
        _workflows.clear()

    def tearDown(self):
        from ..core import _workflows
        _workflows.clear()

    def test_completion_display_metadata(self):
        """Test that completion display metadata is stored correctly"""

        @workflow("completion_display_test")
        class CompletionDisplayWorkflow:
            class Context(BaseModel):
                user_name: str = ""

            @step(start=True)
            def start_step(self):
                ctx = get_context()
                ctx.user_name = "John"
                return complete(
                    display_title="Welcome Complete!",
                    display_subtitle=f"User {ctx.user_name} has been onboarded successfully"
                )

        run = engine.start("completion_display_test")
        engine.execute_step(run.id, "start_step")
        run.refresh_from_db()

        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.completion_display['display_title'], "Welcome Complete!")
        self.assertIn("John", run.completion_display['display_subtitle'])
        self.assertEqual(run.waiting_display, {})

    def test_completion_display_in_current_step_display(self):
        """Test that current_step_display returns completion_display when completed"""

        @workflow("completion_property_test")
        class CompletionPropertyWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True)
            def complete_step(self):
                return complete(
                    display_title="Success!",
                    display_subtitle="All done"
                )

        run = engine.start("completion_property_test")
        engine.execute_step(run.id, "complete_step")
        run.refresh_from_db()

        display = run.current_step_display
        self.assertEqual(display['status'], WorkflowStatus.COMPLETED)
        self.assertIsNotNone(display['completion_display'])
        self.assertEqual(display['completion_display']['display_title'], "Success!")
        self.assertEqual(display['completion_display']['display_subtitle'], "All done")
        # Should return early, so step_display should be None
        self.assertIsNone(display['step_display'])

    def test_wait_for_event_display_metadata(self):
        """Test that wait_for_event display metadata is stored correctly"""

        @workflow("wait_display_test")
        class WaitDisplayWorkflow:
            class Context(BaseModel):
                property_name: str = "Test Property"

            @step(start=True)
            def start_step(self):
                return goto(self.wait_step)

            @wait_for_event(
                "oauth_completed",
                timeout=timedelta(minutes=10),
                display_title="Waiting for Authorization",
                display_subtitle="Please complete OAuth in the popup",
                display_waiting_for="OAuth authorization"
            )
            @step()
            def wait_step(self):
                return complete()

        run = engine.start("wait_display_test")
        engine.execute_step(run.id, "start_step")
        run.refresh_from_db()

        self.assertEqual(run.status, WorkflowStatus.SUSPENDED)
        self.assertEqual(run.current_step, "wait_step")
        self.assertEqual(run.waiting_display['display_title'], "Waiting for Authorization")
        self.assertEqual(run.waiting_display['display_subtitle'], "Please complete OAuth in the popup")
        self.assertEqual(run.waiting_display['display_waiting_for'], "OAuth authorization")

    def test_wait_for_event_as_start_step(self):
        """Test that waiting display is set when wait_for_event is the start step"""

        @workflow("wait_start_test")
        class WaitStartWorkflow:
            class Context(BaseModel):
                pass

            @wait_for_event(
                "trigger_event",
                display_title="Waiting to Start",
                display_subtitle="Waiting for trigger event",
                display_waiting_for="Initial trigger"
            )
            @step(start=True)
            def wait_to_start(self):
                return complete()

        run = engine.start("wait_start_test")

        self.assertEqual(run.status, WorkflowStatus.SUSPENDED)
        self.assertEqual(run.current_step, "wait_to_start")
        self.assertEqual(run.waiting_display['display_title'], "Waiting to Start")
        self.assertEqual(run.waiting_display['display_subtitle'], "Waiting for trigger event")
        self.assertEqual(run.waiting_display['display_waiting_for'], "Initial trigger")

    def test_waiting_display_in_current_step_display(self):
        """Test that current_step_display returns waiting_display when suspended"""

        @workflow("waiting_property_test")
        class WaitingPropertyWorkflow:
            class Context(BaseModel):
                pass

            @wait_for_event(
                "test_event",
                display_title="Please Wait",
                display_subtitle="Processing your request",
                display_waiting_for="External system response"
            )
            @step(start=True)
            def wait_step(self):
                return complete()

        run = engine.start("waiting_property_test")

        display = run.current_step_display
        self.assertEqual(display['status'], WorkflowStatus.SUSPENDED)
        self.assertIsNotNone(display['waiting_display'])
        self.assertEqual(display['waiting_display']['display_title'], "Please Wait")
        self.assertEqual(display['waiting_display']['display_subtitle'], "Processing your request")
        self.assertEqual(display['waiting_display']['display_waiting_for'], "External system response")

    def test_step_display_metadata_with_field_groups(self):
        """Test that step display metadata with field groups is returned correctly"""

        class TestSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            property_name = serializers.CharField()
            property_type = serializers.CharField()
            email = serializers.CharField()

        @workflow("step_display_test")
        class StepDisplayWorkflow:
            class Context(BaseModel):
                property_name: str = ""

            @statezero_action(
                name="collect_property_info",
                serializer=TestSerializer
            )
            @step(
                start=True,
                display=StepDisplayMetadata(
                    display_title="Set Up Your Property",
                    display_description="Enter property details to continue",
                    field_groups=[
                        FieldGroup(
                            display_title="Property Information",
                            display_description="Basic details about your listing",
                            field_names=["property_name", "property_type"]
                        ),
                        FieldGroup(
                            display_title="Contact Details",
                            display_description="How guests can reach you",
                            field_names=["email"]
                        )
                    ]
                )
            )
            def collect_info(self, property_name: str, property_type: str, email: str):
                ctx = get_context()
                ctx.property_name = property_name
                return complete()

        run = engine.start("step_display_test")

        display = run.current_step_display
        self.assertIsNotNone(display['step_display'])
        self.assertEqual(display['step_display']['display_title'], "Set Up Your Property")
        self.assertEqual(display['step_display']['display_description'], "Enter property details to continue")
        
        # Check field groups
        field_groups = display['step_display']['field_groups']
        self.assertEqual(len(field_groups), 2)
        self.assertEqual(field_groups[0]['display_title'], "Property Information")
        self.assertEqual(field_groups[0]['field_names'], ["property_name", "property_type"])
        self.assertEqual(field_groups[1]['display_title'], "Contact Details")
        self.assertEqual(field_groups[1]['field_names'], ["email"])

    def test_step_display_metadata_with_field_configs(self):
        """Test that field display configs are returned correctly"""

        class TestSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            amenities = serializers.ListField(child=serializers.IntegerField())
            address = serializers.CharField()

        @workflow("field_config_test")
        class FieldConfigWorkflow:
            class Context(BaseModel):
                pass

            @statezero_action(
                name="select_amenities",
                serializer=TestSerializer
            )
            @step(
                start=True,
                display=StepDisplayMetadata(
                    display_title="Select Amenities",
                    display_description="Choose available amenities",
                    field_display_configs=[
                        FieldDisplayConfig(
                            field_name="amenities",
                            display_component="AmenitiesMultiSelect",
                            filter_queryset={"is_active": True, "category": "essential"},
                            display_help_text="Select all that apply"
                        ),
                        FieldDisplayConfig(
                            field_name="address",
                            display_component="AddressAutocomplete",
                            display_help_text="Start typing your address"
                        )
                    ]
                )
            )
            def select_amenities_step(self, amenities: list, address: str):
                return complete()

        run = engine.start("field_config_test")

        display = run.current_step_display
        configs = display['step_display']['field_display_configs']
        
        self.assertEqual(len(configs), 2)
        
        amenities_config = configs[0]
        self.assertEqual(amenities_config['field_name'], "amenities")
        self.assertEqual(amenities_config['display_component'], "AmenitiesMultiSelect")
        self.assertEqual(amenities_config['filter_queryset'], {"is_active": True, "category": "essential"})
        self.assertEqual(amenities_config['display_help_text'], "Select all that apply")
        
        address_config = configs[1]
        self.assertEqual(address_config['field_name'], "address")
        self.assertEqual(address_config['display_component'], "AddressAutocomplete")
        self.assertEqual(address_config['display_help_text'], "Start typing your address")

    def test_waiting_display_cleared_on_goto(self):
        """Test that waiting_display is cleared when transitioning to a normal step"""

        @workflow("clear_waiting_test")
        class ClearWaitingWorkflow:
            class Context(BaseModel):
                pass

            @wait_for_event(
                "start_event",
                display_title="Waiting to Start",
                display_subtitle="Please trigger the event",
                display_waiting_for="Start trigger"
            )
            @step(start=True)
            def wait_step(self):
                return goto(self.normal_step)

            @step()
            def normal_step(self):
                return complete(
                    display_title="All Done",
                    display_subtitle="Successfully completed"
                )

        run = engine.start("clear_waiting_test")
        
        # Initially has waiting display
        self.assertNotEqual(run.waiting_display, {})
        self.assertEqual(run.waiting_display['display_title'], "Waiting to Start")

        # Signal to proceed
        engine.signal("event:start_event")
        run.refresh_from_db()
        
        # Execute the wait step (which will goto normal_step and complete due to SynchronousExecutor)
        engine.execute_step(run.id, "wait_step")
        run.refresh_from_db()

        # With SynchronousExecutor, workflow completes immediately
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        # Waiting display should be cleared on completion
        self.assertEqual(run.waiting_display, {})
        # Should have completion display
        self.assertEqual(run.completion_display['display_title'], "All Done")

    def test_completion_clears_waiting_display(self):
        """Test that completion_display clears waiting_display"""

        @workflow("clear_on_complete_test")
        class ClearOnCompleteWorkflow:
            class Context(BaseModel):
                pass

            @wait_for_event(
                "event1",
                display_title="Waiting",
                display_subtitle="Please wait",
                display_waiting_for="Event"
            )
            @step(start=True)
            def wait_step(self):
                return complete(
                    display_title="Done!",
                    display_subtitle="Completed successfully"
                )

        run = engine.start("clear_on_complete_test")
        
        # Initially has waiting display
        self.assertNotEqual(run.waiting_display, {})

        # Signal and complete
        engine.signal("event:event1")
        run.refresh_from_db()
        engine.execute_step(run.id, "wait_step")
        run.refresh_from_db()

        # Should have completion display and empty waiting display
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.completion_display['display_title'], "Done!")
        self.assertEqual(run.waiting_display, {})

    def test_dynamic_completion_message_with_context(self):
        """Test that completion messages can use workflow context for dynamic content"""

        @workflow("dynamic_completion_test")
        class DynamicCompletionWorkflow:
            class Context(BaseModel):
                user_name: str = ""
                items_processed: int = 0

            @step(start=True)
            def process_items(self):
                ctx = get_context()
                ctx.user_name = "Alice"
                ctx.items_processed = 42
                return complete(
                    display_title=f"Processing Complete, {ctx.user_name}!",
                    display_subtitle=f"Successfully processed {ctx.items_processed} items"
                )

        run = engine.start("dynamic_completion_test")
        engine.execute_step(run.id, "process_items")
        run.refresh_from_db()

        self.assertEqual(run.completion_display['display_title'], "Processing Complete, Alice!")
        self.assertEqual(run.completion_display['display_subtitle'], "Successfully processed 42 items")

    def test_none_values_in_display_metadata(self):
        """Test that None values in display metadata are handled gracefully"""

        @workflow("none_display_test")
        class NoneDisplayWorkflow:
            class Context(BaseModel):
                pass

            @wait_for_event(
                "test_event",
                display_title="Just a Title",
                # No subtitle or waiting_for
            )
            @step(start=True)
            def wait_with_partial_display(self):
                return complete(
                    display_title="Just Title Again"
                    # No subtitle
                )

        run = engine.start("none_display_test")

        # Should have title but None for other fields
        self.assertEqual(run.waiting_display['display_title'], "Just a Title")
        self.assertIsNone(run.waiting_display['display_subtitle'])
        self.assertIsNone(run.waiting_display['display_waiting_for'])

        # Complete
        engine.signal("event:test_event")
        run.refresh_from_db()
        engine.execute_step(run.id, "wait_with_partial_display")
        run.refresh_from_db()

        self.assertEqual(run.completion_display['display_title'], "Just Title Again")
        self.assertIsNone(run.completion_display['display_subtitle'])

    def test_step_type_detection_with_display_metadata(self):
        """Test that step_type is correctly identified alongside display metadata"""

        class ActionSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            data = serializers.CharField()

        @workflow("step_type_display_test")
        class StepTypeDisplayWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True)
            def automated_step(self):
                return goto(self.action_step)

            @statezero_action(name="test_action", serializer=ActionSerializer)
            @step(
                display=StepDisplayMetadata(
                    display_title="Manual Input Required",
                    display_description="Please provide information"
                )
            )
            def action_step(self, data: str):
                return goto(self.wait_step)

            @wait_for_event(
                "external_event",
                display_title="Waiting for External System",
                display_subtitle="Please wait while we process",
                display_waiting_for="External API response"
            )
            @step()
            def wait_step(self):
                return complete()

        run = engine.start("step_type_display_test")

        # Automated step
        display = run.current_step_display
        self.assertEqual(display['step_type'], "automated")
        self.assertIsNone(display['step_display'])

        # Move to action step
        engine.execute_step(run.id, "automated_step")
        run.refresh_from_db()
        display = run.current_step_display
        self.assertEqual(display['step_type'], "action")
        self.assertIsNotNone(display['step_display'])
        self.assertEqual(display['step_display']['display_title'], "Manual Input Required")

        # Simulate action completion to move to wait step
        from ..core import _workflows, WorkflowContextManager, _current_context
        workflow_cls = _workflows["step_type_display_test"]
        ctx_manager = WorkflowContextManager(run.id, workflow_cls.Context)
        token = _current_context.set(ctx_manager)
        try:
            workflow_instance = workflow_cls()
            result = workflow_instance.action_step(data="test")
            ctx_manager.commit_changes()
            engine._handle_result(run, result)
        finally:
            _current_context.reset(token)

        run.refresh_from_db()
        display = run.current_step_display
        self.assertEqual(display['step_type'], "waiting")
        self.assertIsNotNone(display['waiting_display'])
        self.assertEqual(display['waiting_display']['display_title'], "Waiting for External System")