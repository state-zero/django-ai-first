from datetime import datetime, timedelta
from typing import Optional

from .q2_executor import Q2Executor, TASK_PATH, DELAYED_TASK_PREFIX, DEBOUNCE_TASK_PREFIX


class SynchronousExecutor(Q2Executor):
    """
    Executes tasks synchronously for integration tests.

    Extends Q2Executor to use the same task dispatch and database models,
    but executes immediate tasks synchronously instead of queuing them.
    Delayed and debounced tasks use the same Schedule/DebouncedTask models
    as production, allowing time-travel testing via process_delayed_tasks().
    """

    @property
    def _delayed_tasks(self):
        """Backwards-compatible property that returns delayed tasks from the Schedule model."""
        from django_q.models import Schedule

        tasks = []
        for sched in Schedule.objects.filter(
            schedule_type=Schedule.ONCE,
            name__startswith=DELAYED_TASK_PREFIX,
        ):
            import ast
            args = ast.literal_eval(sched.args) if sched.args else ()
            # Extract task_name from func path (e.g., "django_ai...tasks.execute_step" -> "execute_step")
            task_name = sched.func.rsplit(".", 1)[-1]
            tasks.append((sched.next_run, task_name, args))
        return tasks

    @property
    def _debounced_tasks(self):
        """Backwards-compatible property that returns debounced tasks from the DebouncedTask model."""
        from .models import DebouncedTask

        return {
            task.debounce_key: task
            for task in DebouncedTask.objects.all()
        }

    def queue_task(self, task_name: str, *args, delay: Optional[timedelta] = None):
        """Execute tasks synchronously, or schedule for later if delayed."""
        from django_q.tasks import async_task

        if delay:
            # Use parent's scheduling (writes to Schedule model)
            return super().queue_task(task_name, *args, delay=delay)

        # Execute synchronously using Django Q2's sync mode
        fn = (
            f"{TASK_PATH}.execute_step"
            if task_name == "execute_step"
            else f"{TASK_PATH}.{task_name}"
        )
        return async_task(fn, *args, sync=True)

    def process_delayed_tasks(self, now: datetime) -> int:
        """
        Process delayed tasks that are due.

        Uses the parent Q2Executor implementation which queries the
        Schedule and DebouncedTask models directly.
        """
        return super().process_delayed_tasks(now)
