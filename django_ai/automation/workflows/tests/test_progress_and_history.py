# django_ai/automation/workflows/tests/test_progress_and_history.py

from datetime import timedelta
from django.test import TestCase
from pydantic import BaseModel
from rest_framework import serializers

from ..core import (
    workflow,
    step,
    goto,
    sleep,
    complete,
    fail,
    engine,
    
    Retry,
)
from ..models import WorkflowRun, WorkflowStatus, StepType
from ..statezero_action import statezero_action
from ...testing import time_machine
from ...queues.sync_executor import SynchronousExecutor


class MockExecutor:
    """Mock executor that queues but doesn't auto-execute"""
    def __init__(self):
        self.queued_tasks = []

    def queue_task(self, task_name, *args, delay=None):
        self.queued_tasks.append({"task_name": task_name, "args": args, "delay": delay})

    def clear(self):
        self.queued_tasks.clear()


class TestProgressHistoryAndStepType(TestCase):
    """Test progress tracking, step execution history, and step type detection"""

    def setUp(self):
        self.executor = MockExecutor()
        engine.set_executor(self.executor)
        from ..core import _workflows
        _workflows.clear()

    def tearDown(self):
        from ..core import _workflows
        _workflows.clear()

    def test_progress_and_step_executions(self):
        """Test that progress updates correctly and step executions are recorded"""

        @workflow("progress_test")
        class ProgressWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True)
            def step_one(self):
                return goto(self.step_two, progress=0.33)

            @step()
            def step_two(self):
                return goto(self.step_three, progress=0.67)

            @step()
            def step_three(self):
                return complete()

        run = engine.start("progress_test")
        self.assertEqual(run.progress, 0.0)
        self.assertEqual(run.step_executions.count(), 0)

        # Execute step one
        engine.execute_step(run.id, "step_one")
        run.refresh_from_db()
        self.assertAlmostEqual(run.progress, 0.33, places=2)
        self.assertEqual(run.step_executions.count(), 1)
        self.assertEqual(run.step_executions.first().step_name, "step_one")
        self.assertEqual(run.step_executions.first().status, "completed")

        # Execute step two
        engine.execute_step(run.id, "step_two")
        run.refresh_from_db()
        self.assertAlmostEqual(run.progress, 0.67, places=2)
        self.assertEqual(run.step_executions.count(), 2)

        # Execute step three - complete should set progress to 1.0
        engine.execute_step(run.id, "step_three")
        run.refresh_from_db()
        self.assertEqual(run.progress, 1.0)
        self.assertEqual(run.step_executions.count(), 3)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)

        # Verify chronological order
        executions = list(run.step_executions.all())
        self.assertEqual([e.step_name for e in executions], ["step_one", "step_two", "step_three"])

    def test_sleep_and_fail_no_execution_on_sleep(self):
        """Test that sleep doesn't create executions but fail does"""

        @workflow("sleep_fail_test", default_retry=Retry(max_attempts=1))
        class SleepFailWorkflow:
            class Context(BaseModel):
                should_fail: bool = False

            @step(start=True)
            def sleep_step(self):
                return goto(self.maybe_fail)

            @step()
            def maybe_fail(self):
                
                if self.context.should_fail:
                    return fail("Test failure")
                return sleep(timedelta(hours=1))

        # Test sleep path
        run1 = engine.start("sleep_fail_test", should_fail=False)
        engine.execute_step(run1.id, "sleep_step")
        engine.execute_step(run1.id, "maybe_fail")
        run1.refresh_from_db()
        
        self.assertEqual(run1.status, WorkflowStatus.WAITING)
        self.assertEqual(run1.step_executions.count(), 1)  # Only sleep_step completed
        self.assertEqual(run1.step_executions.first().step_name, "sleep_step")

        # Test fail path
        run2 = engine.start("sleep_fail_test", should_fail=True)
        engine.execute_step(run2.id, "sleep_step")
        engine.execute_step(run2.id, "maybe_fail")
        run2.refresh_from_db()
        
        self.assertEqual(run2.status, WorkflowStatus.FAILED)
        self.assertEqual(run2.step_executions.count(), 2)
        failed_exec = run2.step_executions.last()
        self.assertEqual(failed_exec.step_name, "maybe_fail")
        self.assertEqual(failed_exec.status, "failed")
        self.assertEqual(failed_exec.error, "Test failure")

    def test_current_step_type_property(self):
        """Test that current_step_type correctly identifies action, automated, and waiting steps"""

        class TestSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            data = serializers.CharField()

        @workflow("step_type_test")
        class StepTypeWorkflow:
            class Context(BaseModel):
                data: str = ""

            @step(start=True)
            def automated_step(self):
                return goto(self.action_step)

            @statezero_action(name="test_action", serializer=TestSerializer)
            @step()
            def action_step(self, data: str):
                
                self.context.data = data
                return goto(self.automated_step_2)

            @step()
            def automated_step_2(self):
                return complete()

        run = engine.start("step_type_test")

        # Initial step is automated
        self.assertEqual(run.current_step, "automated_step")
        self.assertEqual(run.current_step_display['step_type'], StepType.AUTOMATED)

        # Move to action step
        engine.execute_step(run.id, "automated_step")
        run.refresh_from_db()
        self.assertEqual(run.current_step, "action_step")
        self.assertEqual(run.current_step_display['step_type'], StepType.ACTION)
        self.assertEqual(run.status, WorkflowStatus.SUSPENDED)

        # Simulate action completion
        from ..core import _workflows, safe_model_dump
        workflow_cls = _workflows["step_type_test"]
        workflow_instance = workflow_cls()
        workflow_instance.context = workflow_cls.Context.model_validate(run.data)
        result = workflow_instance.action_step(data="test")
        run.data = safe_model_dump(workflow_instance.context)
        run.save()
        engine._handle_result(run, result)

        run.refresh_from_db()
        self.assertEqual(run.current_step, "automated_step_2")
        self.assertEqual(run.current_step_display['step_type'], StepType.AUTOMATED)

    def test_step_execution_captures_step_display(self):
        """Test that StepExecution records capture step_display metadata including visibility"""

        class TestSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()

        @workflow("step_display_capture_test")
        class StepDisplayWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True, title="Visible Step", visible=True)
            def visible_step(self):
                return goto(self.hidden_step)

            @step(title="Hidden Step", visible=False)
            def hidden_step(self):
                return goto(self.action_step)

            @statezero_action(name="test_display_action", serializer=TestSerializer)
            @step(title="Action Step")
            def action_step(self):
                return goto(self.final_step)

            @step()
            def final_step(self):
                return complete()

        run = engine.start("step_display_capture_test")

        # Execute visible_step
        engine.execute_step(run.id, "visible_step")
        run.refresh_from_db()

        exec1 = run.step_executions.get(step_name="visible_step")
        self.assertEqual(exec1.step_display['visible'], True)
        self.assertEqual(exec1.step_display['step_title'], "Visible Step")
        self.assertEqual(exec1.step_display['step_type'], StepType.AUTOMATED)

        # Execute hidden_step
        engine.execute_step(run.id, "hidden_step")
        run.refresh_from_db()

        exec2 = run.step_executions.get(step_name="hidden_step")
        self.assertEqual(exec2.step_display['visible'], False)
        self.assertEqual(exec2.step_display['step_title'], "Hidden Step")
        self.assertEqual(exec2.step_display['step_type'], StepType.AUTOMATED)

        # Action step is suspended, simulate action completion
        self.assertEqual(run.status, WorkflowStatus.SUSPENDED)
        from ..core import _workflows, safe_model_dump
        workflow_cls = _workflows["step_display_capture_test"]
        workflow_instance = workflow_cls()
        workflow_instance.context = workflow_cls.Context.model_validate(run.data)
        result = workflow_instance.action_step()
        run.data = safe_model_dump(workflow_instance.context)
        run.save()
        engine._handle_result(run, result)

        run.refresh_from_db()
        exec3 = run.step_executions.get(step_name="action_step")
        self.assertEqual(exec3.step_display['visible'], True)  # Default visibility
        self.assertEqual(exec3.step_display['step_title'], "Action Step")
        self.assertEqual(exec3.step_display['step_type'], StepType.ACTION)

        # Execute final_step
        engine.execute_step(run.id, "final_step")
        run.refresh_from_db()

        exec4 = run.step_executions.get(step_name="final_step")
        self.assertEqual(exec4.step_display['visible'], True)  # Default
        self.assertEqual(exec4.step_display['step_title'], None)  # No title set
        self.assertEqual(exec4.step_display['step_type'], StepType.AUTOMATED)

        # Verify we can filter by visibility
        visible_executions = [e for e in run.step_executions.all() if e.step_display.get('visible', True)]
        hidden_executions = [e for e in run.step_executions.all() if not e.step_display.get('visible', True)]

        self.assertEqual(len(visible_executions), 3)
        self.assertEqual(len(hidden_executions), 1)
        self.assertEqual(hidden_executions[0].step_name, "hidden_step")


class TestProgressWithTimeMachine(TestCase):
    """Test progress tracking with time machine integration"""

    def setUp(self):
        from ..core import _workflows
        _workflows.clear()

    def tearDown(self):
        from ..core import _workflows
        _workflows.clear()

    def test_progress_updates_through_sleep_wake_cycle(self):
        """Test that progress updates correctly when workflow sleeps and wakes"""

        @workflow("progress_sleep_test")
        class ProgressSleepWorkflow:
            class Context(BaseModel):
                slept: bool = False
                completed: bool = False

            @step(start=True)
            def initial(self):
                return goto(self.process, progress=0.25)

            @step()
            def process(self):
                
                if self.context.slept:
                    return goto(self.finalize, progress=0.75)
                self.context.slept = True
                return sleep(timedelta(hours=2))

            @step()
            def finalize(self):
                
                self.context.completed = True
                return complete()

        with time_machine() as tm:
            executor = SynchronousExecutor()
            engine.set_executor(executor)

            run = engine.start("progress_sleep_test")
            self.assertEqual(run.progress, 0.0)

            # Process first step
            tm.process()
            run.refresh_from_db()
            self.assertAlmostEqual(run.progress, 0.25, places=2)
            self.assertEqual(run.step_executions.count(), 1)

            # Process enters sleep
            tm.process()
            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.WAITING)
            # Progress stays at 0.25 since we didn't set progress on sleep
            self.assertAlmostEqual(run.progress, 0.25, places=2)
            # Sleep doesn't create an execution record
            self.assertEqual(run.step_executions.count(), 1)

            # Advance time to wake up
            tm.advance(hours=3)
            run.refresh_from_db()

            # Should be completed
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)
            self.assertEqual(run.progress, 1.0)
            self.assertTrue(run.data["completed"])
            # Three steps were executed (initial, process after wake, finalize)
            self.assertEqual(run.step_executions.count(), 3)

    def test_step_executions_recorded_through_full_workflow(self):
        """Test that all step executions are properly recorded when using time machine"""

        @workflow("full_execution_test")
        class FullExecutionWorkflow:
            class Context(BaseModel):
                steps_executed: list = []

            @classmethod
            def create_context(cls):
                return cls.Context(steps_executed=[])

            @step(start=True, title="Start Processing")
            def step_one(self):
                
                self.context.steps_executed.append("step_one")
                return goto(self.step_two, progress=0.33)

            @step(title="Continue Processing")
            def step_two(self):
                
                self.context.steps_executed.append("step_two")
                return goto(self.step_three, progress=0.67)

            @step(title="Finalize")
            def step_three(self):
                
                self.context.steps_executed.append("step_three")
                return complete()

        with time_machine() as tm:
            executor = SynchronousExecutor()
            engine.set_executor(executor)

            run = engine.start("full_execution_test")
            tm.run_until_idle()

            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)
            self.assertEqual(run.progress, 1.0)
            self.assertEqual(run.data["steps_executed"], ["step_one", "step_two", "step_three"])

            # Verify execution records
            executions = list(run.step_executions.all())
            self.assertEqual(len(executions), 3)
            self.assertEqual([e.step_name for e in executions], ["step_one", "step_two", "step_three"])

            # Verify step_display was captured
            self.assertEqual(executions[0].step_display['step_title'], "Start Processing")
            self.assertEqual(executions[1].step_display['step_title'], "Continue Processing")
            self.assertEqual(executions[2].step_display['step_title'], "Finalize")