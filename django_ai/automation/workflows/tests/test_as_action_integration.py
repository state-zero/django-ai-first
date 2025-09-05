# tests/test_as_action_integration.py

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status, serializers
from pydantic import BaseModel

from django_ai.automation.workflows.core import (
    workflow,
    step,
    get_context,
    goto,
    complete,
    engine,
    _workflows,
)
from django_ai.automation.workflows.as_action import as_action
from django_ai.automation.workflows.models import WorkflowRun, WorkflowStatus
from django_ai.automation.queues.sync_executor import SynchronousExecutor


class AsActionStateZeroTest(TestCase):
    """Test @as_action integration with StateZero action endpoints"""

    def setUp(self):
        engine.set_executor(SynchronousExecutor())
        _workflows.clear()
        self.client = APIClient()

    def tearDown(self):
        _workflows.clear()

    def test_as_action_full_statezero_integration(self):
        """Test @as_action with real StateZero action endpoint"""

        # Define custom serializer
        class ReviewInputSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            reviewer_notes = serializers.CharField(max_length=500)
            priority = serializers.ChoiceField(choices=["low", "medium", "high"])

        @workflow("expense_approval_workflow")
        class ExpenseApprovalWorkflow:
            class Context(BaseModel):
                expense_id: int = 123
                reviewer_notes: str = ""
                priority: str = "medium"
                status: str = "pending"

            @classmethod
            def create_context(cls):
                return cls.Context()

            @step(start=True)
            def start_review(self):
                return goto(self.await_review)

            @as_action(name="expense_submit_review", serializer=ReviewInputSerializer)
            @step()
            def await_review(self, reviewer_notes: str, priority: str):
                """Step callable as StateZero action"""
                ctx = get_context()
                ctx.reviewer_notes = reviewer_notes
                ctx.priority = priority
                ctx.status = "reviewed"
                return goto(self.complete_process)

            @step()
            def complete_process(self):
                return complete()

        # 1. Start the workflow
        run = engine.start("expense_approval_workflow")

        # 2. Progress to await_review step
        engine.execute_step(run.id, "start_review")
        run.refresh_from_db()

        self.assertEqual(run.current_step, "await_review")
        self.assertEqual(run.status, WorkflowStatus.WAITING.value)

        # 3. Call the StateZero action endpoint
        action_url = reverse(
            "statezero:action", kwargs={"action_name": "expense_submit_review"}
        )

        response = self.client.post(
            action_url,
            {
                "workflow_run_id": run.id,
                "reviewer_notes": "All receipts verified, looks good!",
                "priority": "high",
            },
            format="json",
        )

        # 4. Verify StateZero response
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_data = response.json()
        self.assertEqual(response_data["status"], "success")
        self.assertIn("await_review executed", response_data["message"])

        # 5. Verify workflow progressed
        run.refresh_from_db()
        self.assertEqual(run.current_step, "complete_process")
        self.assertEqual(run.status, WorkflowStatus.WAITING.value)

        # 6. Verify context updated correctly
        self.assertEqual(
            run.data["reviewer_notes"], "All receipts verified, looks good!"
        )
        self.assertEqual(run.data["priority"], "high")
        self.assertEqual(run.data["status"], "reviewed")

    def test_as_action_validation_via_statezero(self):
        """Test StateZero validation with @as_action"""

        class StrictInputSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            amount = serializers.DecimalField(
                max_digits=10, decimal_places=2, min_value=0
            )
            category = serializers.ChoiceField(choices=["travel", "meals", "supplies"])

        @workflow("validation_workflow")
        class ValidationWorkflow:
            class Context(BaseModel):
                amount: float = 0.0
                category: str = ""

            @as_action(name="expense_set_details", serializer=StrictInputSerializer)
            @step(start=True)
            def set_details(self, amount: float, category: str):
                ctx = get_context()
                ctx.amount = amount
                ctx.category = category
                return complete()

        # Start workflow
        run = engine.start("validation_workflow")

        # Test invalid input via StateZero endpoint
        action_url = reverse(
            "statezero:action", kwargs={"action_name": "expense_set_details"}
        )

        response = self.client.post(
            action_url,
            {
                "workflow_run_id": run.id,
                "amount": -100.50,  # Invalid: negative amount
                "category": "invalid_category",  # Invalid: not in choices
            },
            format="json",
        )

        # Should return validation errors
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response_data = response.json()
        self.assertIn("amount", response_data['detail'])  # Amount validation error
        self.assertIn("category", response_data['detail'])  # Category validation error

    def test_as_action_nonexistent_workflow_via_statezero(self):
        """Test StateZero action with nonexistent workflow run"""

        @workflow("nonexistent_test_workflow")
        class NonexistentTestWorkflow:
            class Context(BaseModel):
                data: str = ""

            @as_action(name="test_nonexistent_action")
            @step(start=True)
            def test_step(self, data: str):
                ctx = get_context()
                ctx.data = data
                return complete()

        # Call action with nonexistent workflow run ID
        action_url = reverse(
            "statezero:action", kwargs={"action_name": "test_nonexistent_action"}
        )

        response = self.client.post(
            action_url,
            {"workflow_run_id": 99999, "data": "test"},  # Nonexistent
            format="json",
        )

        # Should return 404 for nonexistent resource (proper HTTP semantics)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        response_data = response.json()
        self.assertEqual(response_data["status"], 404)
        self.assertEqual(response_data["type"], "NotFound")
        self.assertIn("detail", response_data)
