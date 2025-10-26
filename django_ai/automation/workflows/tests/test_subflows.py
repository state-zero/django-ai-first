from django.test import TestCase
from pydantic import BaseModel
from typing import Optional

from ..core import (
    workflow,
    step,
    goto,
    complete,
    fail,
    engine,
    get_context,
    run_subflow,
    SubflowResult,
)
from ..models import WorkflowRun, WorkflowStatus


class MockExecutor:
    """Simple mock executor for testing"""

    def __init__(self):
        self.queued_tasks = []

    def queue_task(self, task_name, *args, delay=None):
        self.queued_tasks.append({"task_name": task_name, "args": args, "delay": delay})

    def clear(self):
        self.queued_tasks.clear()


class SubflowTestCase(TestCase):
    """Test subworkflow composition functionality"""

    def setUp(self):
        self.executor = MockExecutor()
        engine.set_executor(self.executor)

    def tearDown(self):
        from ..core import _workflows, _event_workflows
        _workflows.clear()
        _event_workflows.clear()

    def test_basic_subflow(self):
        """Test basic subworkflow execution"""

        @workflow("sub_workflow")
        class SubWorkflow:
            class Context(BaseModel):
                input_value: int
                output_value: int = 0

            @step(start=True)
            def process(self):
                ctx = get_context()
                ctx.output_value = ctx.input_value * 2
                return complete()

        @workflow("main_workflow")
        class MainWorkflow:
            class Context(BaseModel):
                initial: int
                final: int = 0
                active_subworkflow_run_id: Optional[int] = None

            @step(start=True)
            def start(self):
                ctx = get_context()
                return run_subflow(
                    SubWorkflow,
                    on_complete=self.after_subflow,
                    input_value=ctx.initial
                )

            @step()
            def after_subflow(self, subflow_result: SubflowResult):
                ctx = get_context()
                ctx.final = subflow_result.context.output_value
                return complete()

        # Start main workflow
        main_run = engine.start("main_workflow", initial=5)
        self.assertEqual(main_run.status, WorkflowStatus.RUNNING)

        # Execute the start step which launches the subworkflow
        engine.execute_step(main_run.id, "start")
        main_run.refresh_from_db()

        # Main workflow should be suspended waiting for signal
        self.assertEqual(main_run.status, WorkflowStatus.SUSPENDED)
        self.assertIsNotNone(main_run.active_subworkflow_run_id)
        self.assertEqual(main_run.current_step, "after_subflow")
        self.assertTrue(main_run.waiting_signal.startswith("subflow:"))

        # Get the child workflow
        child_run = WorkflowRun.objects.get(id=main_run.active_subworkflow_run_id)
        self.assertEqual(child_run.name, "sub_workflow")
        self.assertEqual(child_run.parent_run_id, main_run.id)
        self.assertEqual(child_run.data['input_value'], 5)

        # Execute the child workflow's step
        engine.execute_step(child_run.id, "process")
        child_run.refresh_from_db()

        # Child should be completed
        self.assertEqual(child_run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(child_run.data['output_value'], 10)

        # Main workflow should have resumed (but still needs execution)
        main_run.refresh_from_db()
        self.assertEqual(main_run.status, WorkflowStatus.RUNNING)
        self.assertEqual(main_run.waiting_signal, "")

        # The signal() method queued the step, now execute it
        engine.execute_step(main_run.id, "after_subflow")
        main_run.refresh_from_db()

        # Main workflow should be completed
        self.assertEqual(main_run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(main_run.data['final'], 10)

    def test_subflow_failure(self):
        """Test handling of subworkflow failures"""

        @workflow("failing_sub")
        class FailingSubWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True)
            def fail_step(self):
                return fail("Intentional failure")

        @workflow("main_with_failure")
        class MainWorkflow:
            class Context(BaseModel):
                handled: bool = False
                active_subworkflow_run_id: Optional[int] = None

            @step(start=True)
            def start(self):
                return run_subflow(
                    FailingSubWorkflow,
                    on_complete=self.handle_result
                )

            @step()
            def handle_result(self, subflow_result: SubflowResult):
                ctx = get_context()
                if subflow_result.status == WorkflowStatus.FAILED:
                    ctx.handled = True
                return complete()

        main_run = engine.start("main_with_failure")
        engine.execute_step(main_run.id, "start")
        main_run.refresh_from_db()

        # Execute failing child step
        child_run = WorkflowRun.objects.get(id=main_run.active_subworkflow_run_id)
        engine.execute_step(child_run.id, "fail_step")
        child_run.refresh_from_db()

        # Child should be failed
        self.assertEqual(child_run.status, WorkflowStatus.FAILED)

        # Parent should have resumed (but still needs execution)
        main_run.refresh_from_db()
        self.assertEqual(main_run.status, WorkflowStatus.RUNNING)

        # The signal() method queued the step, now execute it
        engine.execute_step(main_run.id, "handle_result")
        main_run.refresh_from_db()
        self.assertEqual(main_run.status, WorkflowStatus.COMPLETED)
        self.assertTrue(main_run.data['handled'])

    def test_nested_subflows(self):
        """Test subworkflows within subworkflows"""

        @workflow("innermost")
        class InnermostWorkflow:
            class Context(BaseModel):
                value: int = 0

            @step(start=True)
            def process(self):
                ctx = get_context()
                ctx.value = 42
                return complete()

        @workflow("middle")
        class MiddleWorkflow:
            class Context(BaseModel):
                final_value: int = 0
                active_subworkflow_run_id: Optional[int] = None

            @step(start=True)
            def run_inner(self):
                return run_subflow(
                    InnermostWorkflow,
                    on_complete=self.process_inner
                )

            @step()
            def process_inner(self, subflow_result: SubflowResult):
                ctx = get_context()
                ctx.final_value = subflow_result.context.value * 2
                return complete()

        @workflow("outer")
        class OuterWorkflow:
            class Context(BaseModel):
                total: int = 0
                active_subworkflow_run_id: Optional[int] = None

            @step(start=True)
            def run_middle(self):
                return run_subflow(
                    MiddleWorkflow,
                    on_complete=self.finish
                )

            @step()
            def finish(self, subflow_result: SubflowResult):
                ctx = get_context()
                ctx.total = subflow_result.context.final_value
                return complete()

        # Start outer workflow
        outer_run = engine.start("outer")
        engine.execute_step(outer_run.id, "run_middle")
        outer_run.refresh_from_db()

        # Get middle workflow
        middle_run = WorkflowRun.objects.get(id=outer_run.active_subworkflow_run_id)
        engine.execute_step(middle_run.id, "run_inner")
        middle_run.refresh_from_db()

        # Get innermost workflow
        innermost_run = WorkflowRun.objects.get(id=middle_run.active_subworkflow_run_id)
        engine.execute_step(innermost_run.id, "process")
        innermost_run.refresh_from_db()
        self.assertEqual(innermost_run.status, WorkflowStatus.COMPLETED)

        # Middle should have resumed (but still needs execution)
        middle_run.refresh_from_db()
        self.assertEqual(middle_run.status, WorkflowStatus.RUNNING)

        # The signal() method queued the step, now execute it
        engine.execute_step(middle_run.id, "process_inner")
        middle_run.refresh_from_db()
        self.assertEqual(middle_run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(middle_run.data['final_value'], 84)

        # Outer should have resumed (but still needs execution)
        outer_run.refresh_from_db()
        self.assertEqual(outer_run.status, WorkflowStatus.RUNNING)

        # The signal() method queued the step, now execute it
        engine.execute_step(outer_run.id, "finish")
        outer_run.refresh_from_db()
        self.assertEqual(outer_run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(outer_run.data['total'], 84)


if __name__ == "__main__":
    import unittest
    unittest.main()
