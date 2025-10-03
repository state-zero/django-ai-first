from typing import Optional
from datetime import timedelta
from django_q.tasks import async_task

TASK_PATH = "django_ai.automation.queues.tasks"

class Q2Executor:
    def queue_task(self, task_name: str, *args, delay: Optional[timedelta] = None):
        fn = (
            f"{TASK_PATH}.execute_step"
            if task_name == "execute_step"
            else f"{TASK_PATH}.{task_name}"
        )
        q_kwargs = {}
        if delay:
            q_kwargs["q_options"] = {"delay": int(delay.total_seconds())}
        return async_task(fn, *args, **q_kwargs)
