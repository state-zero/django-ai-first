from datetime import timedelta
from django.test import TestCase
from pydantic import BaseModel
from typing import Optional

from ..core import (
    workflow,
    step,
    goto,
    sleep,
    complete,
    fail,
    engine,
    
    run_subflow,
    SubflowResult,
)
from ..models import WorkflowRun, WorkflowStatus
from ...testing import time_machine
from ...queues.sync_executor import SynchronousExecutor


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
                
                self.context.output_value = self.context.input_value * 2
                return complete()

        @workflow("main_workflow")
        class MainWorkflow:
            class Context(BaseModel):
                initial: int
                final: int = 0
                active_subworkflow_run_id: Optional[int] = None

            @step(start=True)
            def start(self):
                
                return run_subflow(
                    SubWorkflow,
                    on_complete=self.after_subflow,
                    input_value=self.context.initial
                )

            @step()
            def after_subflow(self, subflow_result: SubflowResult):
                
                self.context.final = subflow_result.context.output_value
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
                
                if subflow_result.status == WorkflowStatus.FAILED:
                    self.context.handled = True
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
                
                self.context.value = 42
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
                
                self.context.final_value = subflow_result.context.value * 2
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
                
                self.context.total = subflow_result.context.final_value
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

    def test_on_create_callback(self):
        """Test that on_create callback is called when subworkflow is created"""

        # Track callback invocations
        callback_tracker = []

        @workflow("child_workflow")
        class ChildWorkflow:
            class Context(BaseModel):
                value: int
                result: int = 0

            @step(start=True)
            def process(self):
                
                self.context.result = self.context.value * 3
                return complete()

        @workflow("parent_workflow")
        class ParentWorkflow:
            class Context(BaseModel):
                input_val: int
                created_child_id: Optional[int] = None
                final_result: int = 0
                active_subworkflow_run_id: Optional[int] = None

            @step(start=True)
            def start(self):
                
                return run_subflow(
                    ChildWorkflow,
                    on_complete=self.handle_result,
                    on_create=self.setup_child,
                    value=self.context.input_val
                )

            @step()
            def setup_child(self, child_run: WorkflowRun, parent_run: WorkflowRun):
                """Called synchronously when child is created"""
                
                self.context.created_child_id = child_run.id
                callback_tracker.append({
                    'child_run_id': child_run.id,
                    'child_status': child_run.status,
                    'child_name': child_run.name
                })

            @step()
            def handle_result(self, subflow_result: SubflowResult):
                
                self.context.final_result = subflow_result.context.result
                return complete()

        # Start parent workflow
        parent_run = engine.start("parent_workflow", input_val=7)
        self.assertEqual(parent_run.status, WorkflowStatus.RUNNING)

        # Execute the start step which launches the subworkflow
        engine.execute_step(parent_run.id, "start")
        parent_run.refresh_from_db()

        # Verify on_create callback was called
        self.assertEqual(len(callback_tracker), 1)
        self.assertIsNotNone(callback_tracker[0]['child_run_id'])
        self.assertEqual(callback_tracker[0]['child_status'], WorkflowStatus.RUNNING)
        self.assertEqual(callback_tracker[0]['child_name'], "child_workflow")

        # Verify the child_run_id was stored in parent context
        self.assertEqual(parent_run.data['created_child_id'], callback_tracker[0]['child_run_id'])

        # Verify parent is suspended
        self.assertEqual(parent_run.status, WorkflowStatus.SUSPENDED)
        self.assertIsNotNone(parent_run.active_subworkflow_run_id)
        self.assertEqual(parent_run.current_step, "handle_result")

        # Get the child workflow and verify it matches the callback data
        child_run = WorkflowRun.objects.get(id=parent_run.active_subworkflow_run_id)
        self.assertEqual(child_run.id, callback_tracker[0]['child_run_id'])
        self.assertEqual(child_run.parent_run_id, parent_run.id)
        self.assertEqual(child_run.data['value'], 7)

        # Execute child workflow
        engine.execute_step(child_run.id, "process")
        child_run.refresh_from_db()
        self.assertEqual(child_run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(child_run.data['result'], 21)

        # Resume and complete parent workflow
        parent_run.refresh_from_db()
        self.assertEqual(parent_run.status, WorkflowStatus.RUNNING)

        engine.execute_step(parent_run.id, "handle_result")
        parent_run.refresh_from_db()
        self.assertEqual(parent_run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(parent_run.data['final_result'], 21)


class TestSubflowsWithTimeMachine(TestCase):
    """Test subflows with time machine for full integration"""

    def tearDown(self):
        from ..core import _workflows, _event_workflows
        _workflows.clear()
        _event_workflows.clear()

    def test_subflow_with_sleep_using_time_machine(self):
        """Test parent workflow waiting for child that sleeps"""

        @workflow("child_sleeps")
        class ChildSleepsWorkflow:
            class Context(BaseModel):
                processed: bool = False
                slept: bool = False

            @step(start=True)
            def start_child(self):
                
                if self.context.slept:
                    return goto(self.finish_child)
                self.context.slept = True
                return sleep(timedelta(hours=1))

            @step()
            def finish_child(self):
                
                self.context.processed = True
                return complete()

        @workflow("parent_with_sleeping_child")
        class ParentWorkflow:
            class Context(BaseModel):
                result: str = ""
                active_subworkflow_run_id: Optional[int] = None

            @step(start=True)
            def start_parent(self):
                return run_subflow(
                    ChildSleepsWorkflow,
                    on_complete=self.after_child
                )

            @step()
            def after_child(self, subflow_result: SubflowResult):
                
                if subflow_result.context.processed:
                    self.context.result = "child processed successfully"
                return complete()

        with time_machine() as tm:
            executor = SynchronousExecutor()
            engine.set_executor(executor)

            # Start parent workflow
            parent_run = engine.start("parent_with_sleeping_child")
            tm.process()
            parent_run.refresh_from_db()

            # Parent should be suspended, waiting for child
            self.assertEqual(parent_run.status, WorkflowStatus.SUSPENDED)
            self.assertIsNotNone(parent_run.active_subworkflow_run_id)

            # Child should be sleeping
            child_run = WorkflowRun.objects.get(id=parent_run.active_subworkflow_run_id)
            self.assertEqual(child_run.status, WorkflowStatus.WAITING)

            # Advance time to wake the child
            tm.advance(hours=2)

            # Both should be complete now
            child_run.refresh_from_db()
            parent_run.refresh_from_db()
            self.assertEqual(child_run.status, WorkflowStatus.COMPLETED)
            self.assertTrue(child_run.data["processed"])
            self.assertEqual(parent_run.status, WorkflowStatus.COMPLETED)
            self.assertEqual(parent_run.data["result"], "child processed successfully")

    def test_nested_subflows_run_to_completion(self):
        """Test that time machine can run nested subflows to completion"""

        @workflow("tm_innermost")
        class InnermostWorkflow:
            class Context(BaseModel):
                value: int = 0

            @step(start=True)
            def process(self):
                
                self.context.value = 100
                return complete()

        @workflow("tm_middle")
        class MiddleWorkflow:
            class Context(BaseModel):
                doubled: int = 0
                active_subworkflow_run_id: Optional[int] = None

            @step(start=True)
            def run_inner(self):
                return run_subflow(InnermostWorkflow, on_complete=self.after_inner)

            @step()
            def after_inner(self, subflow_result: SubflowResult):
                
                self.context.doubled = subflow_result.context.value * 2
                return complete()

        @workflow("tm_outer")
        class OuterWorkflow:
            class Context(BaseModel):
                final: int = 0
                active_subworkflow_run_id: Optional[int] = None

            @step(start=True)
            def run_middle(self):
                return run_subflow(MiddleWorkflow, on_complete=self.finish)

            @step()
            def finish(self, subflow_result: SubflowResult):
                
                self.context.final = subflow_result.context.doubled + 1
                return complete()

        with time_machine() as tm:
            executor = SynchronousExecutor()
            engine.set_executor(executor)

            # Start and run to completion
            outer_run = engine.start("tm_outer")
            tm.run_until_idle()

            outer_run.refresh_from_db()
            self.assertEqual(outer_run.status, WorkflowStatus.COMPLETED)
            # 100 * 2 + 1 = 201
            self.assertEqual(outer_run.data["final"], 201)


if __name__ == "__main__":
    import unittest
    unittest.main()
