# tests/test_statezero_action_integration.py

from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status, serializers
from pydantic import BaseModel

from django_ai.automation.workflows.core import (
    workflow,
    step,
    
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

            @statezero_action(
                name="expense_submit_review", serializer=ReviewInputSerializer
            )
            @step()
            def await_review(self, reviewer_notes: str, priority: str):
                """Step callable as StateZero action"""
                
                self.context.reviewer_notes = reviewer_notes
                self.context.priority = priority
                self.context.status = "reviewed"
                return goto(self.complete_process)

            @step()
            def complete_process(self):
                return complete()

        # 1. Start the workflow and progress to the action step
        run = engine.start("expense_approval_workflow")
        engine.execute_step(run.id, "start_review")
        run.refresh_from_db()

        # 2. Verify workflow is suspended, waiting for the action
        self.assertEqual(run.current_step, "await_review")
        self.assertEqual(run.status, WorkflowStatus.SUSPENDED.value)

        # 3. Call the StateZero action endpoint
        # Get the full action path from the workflow step function
        full_action_name = ExpenseApprovalWorkflow.await_review._full_action_name
        action_url = reverse(
            "statezero:action", kwargs={"action_name": full_action_name}
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

        # 5. Verify workflow progressed and completed
        # Because we use a SynchronousExecutor, the entire chain executes at once.
        run.refresh_from_db()
        self.assertEqual(run.current_step, "complete_process")
        self.assertEqual(run.status, WorkflowStatus.COMPLETED.value)

        # 6. Verify context updated correctly
        self.assertEqual(
            run.data["reviewer_notes"], "All receipts verified, looks good!"
        )
        self.assertEqual(run.data["priority"], "high")
        self.assertEqual(run.data["status"], "reviewed")

    def test_statezero_action_chains_to_hidden_step(self):
        """Test that @statezero_action correctly chains to a hidden step (visible=False)"""

        class ActionSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            user_choice = serializers.CharField()

        @workflow("hidden_step_chain_workflow")
        class HiddenStepChainWorkflow:
            class Context(BaseModel):
                steps_executed: list = []
                user_choice: str = ""

            @classmethod
            def create_context(cls):
                return cls.Context(steps_executed=[])

            @step(start=True)
            def start_step(self):
                
                self.context.steps_executed.append("start_step")
                return goto(self.await_user_action)

            @statezero_action(name="user_action", serializer=ActionSerializer)
            @step()
            def await_user_action(self, user_choice: str):
                
                self.context.steps_executed.append("await_user_action")
                self.context.user_choice = user_choice
                return goto(self.hidden_processing)

            @step(visible=False)
            def hidden_processing(self):
                """Hidden step that should execute after the action"""
                
                self.context.steps_executed.append("hidden_processing")
                return goto(self.final_step)

            @step()
            def final_step(self):
                
                self.context.steps_executed.append("final_step")
                return complete()

        # 1. Start the workflow and progress to the action step
        run = engine.start("hidden_step_chain_workflow")
        engine.execute_step(run.id, "start_step")
        run.refresh_from_db()

        # 2. Verify workflow is suspended at action step
        self.assertEqual(run.current_step, "await_user_action")
        self.assertEqual(run.status, WorkflowStatus.SUSPENDED.value)
        self.assertEqual(run.data["steps_executed"], ["start_step"])

        # 3. Call the StateZero action
        action_url = reverse(
            "statezero:action",
            kwargs={"action_name": HiddenStepChainWorkflow.await_user_action._full_action_name}
        )
        response = self.client.post(
            action_url,
            {"workflow_run_id": run.id, "user_choice": "option_a"},
            format="json",
        )

        # 4. Verify response
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 5. Verify entire chain executed including hidden step
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.COMPLETED.value)
        self.assertEqual(
            run.data["steps_executed"],
            ["start_step", "await_user_action", "hidden_processing", "final_step"]
        )

    def test_statezero_action_validation_via_statezero(self):
        """Test StateZero validation with @statezero_action"""

        class StrictInputSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            amount = serializers.DecimalField(
                max_digits=10, decimal_places=2, min_value=Decimal("0.00")
            )
            category = serializers.ChoiceField(choices=["travel", "meals", "supplies"])

        @workflow("validation_workflow")
        class ValidationWorkflow:
            class Context(BaseModel):
                amount: float = 0.0
                category: str = ""

            @statezero_action(
                name="expense_set_details", serializer=StrictInputSerializer
            )
            @step(start=True)
            def set_details(self, amount: float, category: str):
                
                self.context.amount = amount
                self.context.category = category
                return complete()

        # Start workflow, which will immediately suspend at the action step
        run = engine.start("validation_workflow")
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.SUSPENDED.value)

        # Test invalid input via StateZero endpoint
        action_url = reverse(
            "statezero:action", kwargs={"action_name": ValidationWorkflow.set_details._full_action_name}
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
        self.assertIn("amount", response_data["detail"])
        self.assertIn("category", response_data["detail"])

    def test_statezero_action_nonexistent_workflow_via_statezero(self):
        """Test StateZero action with nonexistent workflow run"""

        @workflow("nonexistent_test_workflow")
        class NonexistentTestWorkflow:
            class Context(BaseModel):
                data: str = ""

            @statezero_action(name="test_nonexistent_action")
            @step(start=True)
            def test_step(self, data: str):
                
                self.context.data = data
                return complete()

        action_url = reverse(
            "statezero:action", kwargs={"action_name": "workflow_NonexistentTestWorkflow_test_nonexistent_action"}
        )
        response = self.client.post(
            action_url,
            {"workflow_run_id": 99999, "data": "test"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response_data = response.json()
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
                
                self.context.user_action = user_action
                self.context.user_authenticated = request is not None
                return complete()

        run = engine.start("user_workflow")
        action_url = reverse(
            "statezero:action", kwargs={"action_name": UserWorkflow.process_user_action._full_action_name}
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
        self.assertTrue(run.data["user_authenticated"])

    def test_statezero_action_no_serializer(self):
        """Test @statezero_action without input serializer"""

        @workflow("no_serializer_workflow")
        class NoSerializerWorkflow:
            class Context(BaseModel):
                executed: bool = False

            @statezero_action(name="simple_action")
            @step(start=True)
            def simple_step(self):
                
                self.context.executed = True
                return complete()

        run = engine.start("no_serializer_workflow")
        action_url = reverse(
            "statezero:action", kwargs={"action_name": NoSerializerWorkflow.simple_step._full_action_name}
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
                
                self.context.amount = amount
                self.context.description = description
                self.context.category = category
                return goto(self.await_manager_review)

            @statezero_action(name="manager_review", serializer=ManagerReviewSerializer)
            @step()
            def await_manager_review(self, approved: bool, notes: str = ""):
                
                self.context.manager_approved = approved
                self.context.manager_notes = notes
                if approved:
                    return goto(self.await_finance_processing)
                else:
                    self.context.final_status = "rejected"
                    return complete()

            @statezero_action(
                name="finance_process", serializer=FinanceProcessSerializer
            )
            @step()
            def await_finance_processing(self, payment_method: str, processed: bool):
                
                self.context.payment_method = payment_method
                if processed:
                    self.context.final_status = "paid"
                    return complete()
                else:
                    return goto(self.await_finance_processing)  # Stay in same step

        # Start the workflow, which suspends at the first step
        run = engine.start("expense_approval_process")
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.SUSPENDED.value)
        self.assertEqual(run.current_step, "submit_expense")

        # Step 1: Submit expense
        submit_url = reverse(
            "statezero:action", kwargs={"action_name": ExpenseApprovalProcess.submit_expense._full_action_name}
        )
        self.client.post(
            submit_url,
            {
                "workflow_run_id": run.id,
                "amount": "150.00",
                "description": "Client dinner",
                "category": "meals",
            },
            format="json",
        )
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.SUSPENDED.value)
        self.assertEqual(run.current_step, "await_manager_review")
        self.assertEqual(run.data["amount"], "150.00")

        # Step 2: Manager approval
        review_url = reverse(
            "statezero:action", kwargs={"action_name": ExpenseApprovalProcess.await_manager_review._full_action_name}
        )
        self.client.post(
            review_url,
            {
                "workflow_run_id": run.id,
                "approved": True,
                "notes": "Approved for client meeting expenses",
            },
            format="json",
        )
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.SUSPENDED.value)
        self.assertEqual(run.current_step, "await_finance_processing")
        self.assertTrue(run.data["manager_approved"])

        # Step 3: Finance processing
        finance_url = reverse(
            "statezero:action", kwargs={"action_name": ExpenseApprovalProcess.await_finance_processing._full_action_name}
        )
        self.client.post(
            finance_url,
            {
                "workflow_run_id": run.id,
                "payment_method": "direct_deposit",
                "processed": True,
            },
            format="json",
        )
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.COMPLETED.value)
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

            @statezero_action(
                name="submit_expense_2", serializer=SubmitExpenseSerializer
            )
            @step(start=True)
            def submit_expense(self, amount: str, description: str, category: str):
                
                self.context.amount = amount
                self.context.description = description
                self.context.category = category
                return goto(self.await_manager_review)

            @statezero_action(
                name="manager_review_2", serializer=ManagerReviewSerializer
            )
            @step()
            def await_manager_review(self, approved: bool, notes: str = ""):
                
                self.context.manager_approved = approved
                self.context.manager_notes = notes
                if approved:
                    self.context.final_status = "approved"
                    return complete()
                else:
                    self.context.final_status = "rejected"
                    return complete()

        # Start the workflow
        run = engine.start("expense_rejection_process")

        # Step 1: Submit expense
        submit_url = reverse(
            "statezero:action", kwargs={"action_name": ExpenseRejectionProcess.submit_expense._full_action_name}
        )
        self.client.post(
            submit_url,
            {
                "workflow_run_id": run.id,
                "amount": "5000.00",
                "description": "Expensive equipment",
                "category": "equipment",
            },
            format="json",
        )
        run.refresh_from_db()
        self.assertEqual(run.current_step, "await_manager_review")
        self.assertEqual(run.status, WorkflowStatus.SUSPENDED.value)

        # Step 2: Manager rejection
        review_url = reverse(
            "statezero:action", kwargs={"action_name": ExpenseRejectionProcess.await_manager_review._full_action_name}
        )
        self.client.post(
            review_url,
            {
                "workflow_run_id": run.id,
                "approved": False,
                "notes": "Amount too high, needs additional approval",
            },
            format="json",
        )
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.COMPLETED.value)
        self.assertEqual(run.data["final_status"], "rejected")
        self.assertFalse(run.data["manager_approved"])
        self.assertEqual(
            run.data["manager_notes"], "Amount too high, needs additional approval"
        )
