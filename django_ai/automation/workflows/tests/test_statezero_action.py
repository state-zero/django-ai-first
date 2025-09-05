# tests/test_statezero_action_integration.py

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
from django_ai.automation.workflows.statezero_action import statezero_action
from django_ai.automation.workflows.models import WorkflowRun, WorkflowStatus
from django_ai.automation.queues.sync_executor import SynchronousExecutor


class AsActionStateZeroTest(TestCase):
    """Test @statezero_action integration with StateZero action endpoints"""

    def setUp(self):
        engine.set_executor(SynchronousExecutor())
        _workflows.clear()
        self.client = APIClient()

    def tearDown(self):
        _workflows.clear()

    def test_statezero_action_full_statezero_integration(self):
        """Test @statezero_action with real StateZero action endpoint"""

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

            @statezero_action(name="expense_submit_review", serializer=ReviewInputSerializer)
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

    def test_statezero_action_validation_via_statezero(self):
        """Test StateZero validation with @statezero_action"""

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

            @statezero_action(name="expense_set_details", serializer=StrictInputSerializer)
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
        self.assertIn("amount", response_data["detail"])  # Amount validation error
        self.assertIn("category", response_data["detail"])  # Category validation error

    def test_statezero_action_nonexistent_workflow_via_statezero(self):
        """Test StateZero action with nonexistent workflow run"""

        @workflow("nonexistent_test_workflow")
        class NonexistentTestWorkflow:
            class Context(BaseModel):
                data: str = ""

            @statezero_action(name="test_nonexistent_action")
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

    def test_statezero_action_with_request_parameter(self):
        """Test @statezero_action step that accepts request parameter"""

        class UserActionSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            user_action = serializers.CharField(max_length=100)

        @workflow("user_workflow")
        class UserWorkflow:
            class Context(BaseModel):
                user_action: str = ""
                user_authenticated: bool = False

            @statezero_action(name="user_action_step", serializer=UserActionSerializer)
            @step(start=True)
            def process_user_action(self, user_action: str, request=None):
                """Step that uses request parameter"""
                ctx = get_context()
                ctx.user_action = user_action
                # In a real workflow, you might check request.user
                ctx.user_authenticated = request is not None
                return complete()

        run = engine.start("user_workflow")

        action_url = reverse(
            "statezero:action", kwargs={"action_name": "user_action_step"}
        )

        response = self.client.post(
            action_url,
            {
                "workflow_run_id": run.id,
                "user_action": "approve_expense",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        run.refresh_from_db()
        self.assertEqual(run.data["user_action"], "approve_expense")
        self.assertTrue(run.data["user_authenticated"])  # request was passed

    def test_statezero_action_no_serializer(self):
        """Test @statezero_action without input serializer"""

        @workflow("no_serializer_workflow")
        class NoSerializerWorkflow:
            class Context(BaseModel):
                executed: bool = False

            @statezero_action(name="simple_action")
            @step(start=True)
            def simple_step(self):
                ctx = get_context()
                ctx.executed = True
                return complete()

        run = engine.start("no_serializer_workflow")

        action_url = reverse(
            "statezero:action", kwargs={"action_name": "simple_action"}
        )

        response = self.client.post(
            action_url,
            {"workflow_run_id": run.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertTrue(run.data["executed"])

    def test_multi_step_expense_approval_workflow(self):
        """Test a complete multi-step workflow with multiple action points"""

        class SubmitExpenseSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            amount = serializers.CharField()
            description = serializers.CharField()
            category = serializers.CharField()

        class ManagerReviewSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            approved = serializers.BooleanField()
            notes = serializers.CharField(required=False)

        class FinanceProcessSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            payment_method = serializers.ChoiceField(
                choices=["direct_deposit", "check"]
            )
            processed = serializers.BooleanField()

        @workflow("expense_approval_process")
        class ExpenseApprovalProcess:
            class Context(BaseModel):
                amount: str = ""
                description: str = ""
                category: str = ""
                manager_approved: bool = False
                manager_notes: str = ""
                payment_method: str = ""
                final_status: str = "pending"

            @statezero_action(name="submit_expense", serializer=SubmitExpenseSerializer)
            @step(start=True)
            def submit_expense(self, amount: str, description: str, category: str):
                ctx = get_context()
                ctx.amount = amount
                ctx.description = description
                ctx.category = category
                return goto(self.await_manager_review)

            @statezero_action(name="manager_review", serializer=ManagerReviewSerializer)
            @step()
            def await_manager_review(self, approved: bool, notes: str = ""):
                ctx = get_context()
                ctx.manager_approved = approved
                ctx.manager_notes = notes

                if approved:
                    return goto(self.await_finance_processing)
                else:
                    ctx.final_status = "rejected"
                    return complete()

            @statezero_action(name="finance_process", serializer=FinanceProcessSerializer)
            @step()
            def await_finance_processing(self, payment_method: str, processed: bool):
                ctx = get_context()
                ctx.payment_method = payment_method

                if processed:
                    ctx.final_status = "paid"
                    return complete()
                else:
                    return goto(self.await_finance_processing)  # Stay in same step

        # Start the workflow
        run = engine.start("expense_approval_process")

        # Step 1: Submit expense
        submit_url = reverse(
            "statezero:action", kwargs={"action_name": "submit_expense"}
        )
        response = self.client.post(
            submit_url,
            {
                "workflow_run_id": run.id,
                "amount": "150.00",
                "description": "Client dinner",
                "category": "meals",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        run.refresh_from_db()
        self.assertEqual(run.current_step, "await_manager_review")
        self.assertEqual(run.data["amount"], "150.00")
        self.assertEqual(run.data["description"], "Client dinner")

        # Step 2: Manager approval
        review_url = reverse(
            "statezero:action", kwargs={"action_name": "manager_review"}
        )
        response = self.client.post(
            review_url,
            {
                "workflow_run_id": run.id,
                "approved": True,
                "notes": "Approved for client meeting expenses",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        run.refresh_from_db()
        self.assertEqual(run.current_step, "await_finance_processing")
        self.assertTrue(run.data["manager_approved"])
        self.assertEqual(
            run.data["manager_notes"], "Approved for client meeting expenses"
        )

        # Step 3: Finance processing
        finance_url = reverse(
            "statezero:action", kwargs={"action_name": "finance_process"}
        )
        response = self.client.post(
            finance_url,
            {
                "workflow_run_id": run.id,
                "payment_method": "direct_deposit",
                "processed": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.data["final_status"], "paid")
        self.assertEqual(run.data["payment_method"], "direct_deposit")

    def test_multi_step_workflow_rejection_path(self):
        """Test the rejection path in a multi-step workflow"""

        class SubmitExpenseSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            amount = serializers.CharField()
            description = serializers.CharField()
            category = serializers.CharField()

        class ManagerReviewSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            approved = serializers.BooleanField()
            notes = serializers.CharField(required=False)

        @workflow("expense_rejection_process")
        class ExpenseRejectionProcess:
            class Context(BaseModel):
                amount: str = ""
                description: str = ""
                category: str = ""
                manager_approved: bool = False
                manager_notes: str = ""
                final_status: str = "pending"

            @statezero_action(name="submit_expense_2", serializer=SubmitExpenseSerializer)
            @step(start=True)
            def submit_expense(self, amount: str, description: str, category: str):
                ctx = get_context()
                ctx.amount = amount
                ctx.description = description
                ctx.category = category
                return goto(self.await_manager_review)

            @statezero_action(name="manager_review_2", serializer=ManagerReviewSerializer)
            @step()
            def await_manager_review(self, approved: bool, notes: str = ""):
                ctx = get_context()
                ctx.manager_approved = approved
                ctx.manager_notes = notes

                if approved:
                    ctx.final_status = "approved"
                    return complete()
                else:
                    ctx.final_status = "rejected"
                    return complete()

        # Start the workflow
        run = engine.start("expense_rejection_process")

        # Step 1: Submit expense
        submit_url = reverse(
            "statezero:action", kwargs={"action_name": "submit_expense_2"}
        )
        response = self.client.post(
            submit_url,
            {
                "workflow_run_id": run.id,
                "amount": "5000.00",
                "description": "Expensive equipment",
                "category": "equipment",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        run.refresh_from_db()
        self.assertEqual(run.current_step, "await_manager_review")

        # Step 2: Manager rejection
        review_url = reverse(
            "statezero:action", kwargs={"action_name": "manager_review_2"}
        )
        response = self.client.post(
            review_url,
            {
                "workflow_run_id": run.id,
                "approved": False,
                "notes": "Amount too high, needs additional approval",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.data["final_status"], "rejected")
        self.assertFalse(run.data["manager_approved"])
        self.assertEqual(
            run.data["manager_notes"], "Amount too high, needs additional approval"
        )
