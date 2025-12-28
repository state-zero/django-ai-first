from typing import Optional
from datetime import timedelta
from django_q.tasks import async_task

TASK_PATH = "django_ai.automation.queues.tasks"


class Q2Executor:
    def queue_task(self, task_name: str, *args, delay: Optional[timedelta] = None):
        """
        Queue a task for execution via Django-Q2.

        If `delay` is specified, the task is scheduled via the timers app
        for precise execution at the specified time. Otherwise, the task
        is queued immediately via Django-Q2.
        """
        fn = (
            f"{TASK_PATH}.execute_step"
            if task_name == "execute_step"
            else f"{TASK_PATH}.{task_name}"
        )

        if delay:
            # Use timers app for precise delayed execution
            from django_ai.automation.timers.core import schedule_task
            return schedule_task(fn, args=list(args), delay=delay)

        return async_task(fn, *args)
