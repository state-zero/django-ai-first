from typing import Optional
from datetime import datetime, timedelta
import ast
import uuid
from importlib import import_module

from django.db import transaction
from django.utils import timezone
from django_q.tasks import async_task, schedule
from django_q.models import Schedule

TASK_PATH = "django_ai.automation.queues.tasks"
DELAYED_TASK_PREFIX = "django-ai-delayed::"
DEBOUNCE_TASK_PREFIX = "django-ai-debounce::"


class Q2Executor:
    def queue_task(self, task_name: str, *args, delay: Optional[timedelta] = None):
        fn = (
            f"{TASK_PATH}.execute_step"
            if task_name == "execute_step"
            else f"{TASK_PATH}.{task_name}"
        )
        if delay:
            return schedule(
                fn,
                *args,
                name=f"{DELAYED_TASK_PREFIX}{task_name}_{uuid.uuid4().hex[:8]}",
                schedule_type="O",
                next_run=timezone.now() + delay,
                repeats=1,
            )
        return async_task(fn, *args)

    def debounce_task(
        self,
        debounce_key: str,
        task_name: str,
        *args,
        delay: timedelta,
        event_id: Optional[int] = None,
    ):
        """
        Schedule a task with debouncing.

        If a task with the same debounce_key is already pending, its execution
        time is pushed forward by the delay. Only when no new calls come in
        for the full delay period will the task actually execute.

        Events are accumulated during the debounce window. When the task finally
        executes, handlers/workflows receive all collected events via the `events`
        kwarg (list of Event objects).

        Args:
            debounce_key: Unique key for deduplication
            task_name: Name of the task to execute
            *args: Arguments to pass to the task
            delay: How long to wait before executing
            event_id: Optional event ID to accumulate
        """
        from .models import DebouncedTask
        import json

        scheduled_at = timezone.now() + delay

        with transaction.atomic():
            task, created = DebouncedTask.objects.select_for_update().get_or_create(
                debounce_key=debounce_key,
                defaults={
                    "task_name": task_name,
                    "task_args_json": json.dumps(list(args)),
                    "event_ids_json": json.dumps([event_id] if event_id else []),
                    "scheduled_at": scheduled_at,
                },
            )

            if not created:
                # Update existing task - push scheduled_at and add event
                task.task_name = task_name
                task.task_args_json = json.dumps(list(args))
                task.scheduled_at = scheduled_at
                if event_id:
                    task.add_event_id(event_id)
                task.save()

        # Always schedule a check task - it will verify if it should run
        return schedule(
            f"{TASK_PATH}.execute_debounced_task",
            debounce_key,
            name=f"{DEBOUNCE_TASK_PREFIX}{debounce_key}_{uuid.uuid4().hex[:8]}",
            schedule_type="O",
            next_run=scheduled_at,
            repeats=1,
        )

    def process_delayed_tasks(self, now: datetime) -> int:
        """Process due one-time schedules (for testing with time machine)."""
        count = 0

        # Process regular delayed tasks
        due_schedules = Schedule.objects.filter(
            schedule_type=Schedule.ONCE,
            name__startswith=DELAYED_TASK_PREFIX,
            next_run__lte=now,
        )

        for sched in due_schedules:
            module_path, func_name = sched.func.rsplit(".", 1)
            module = import_module(module_path)
            func = getattr(module, func_name)

            args = ast.literal_eval(sched.args) if sched.args else ()
            func(*args)

            sched.delete()
            count += 1

        # Process debounced tasks
        count += self.process_debounced_tasks(now)

        return count

    def process_debounced_tasks(self, now: datetime) -> int:
        """Process due debounced tasks (for testing with time machine)."""
        from .models import DebouncedTask

        # Find debounce check schedules that are due
        due_schedules = Schedule.objects.filter(
            schedule_type=Schedule.ONCE,
            name__startswith=DEBOUNCE_TASK_PREFIX,
            next_run__lte=now,
        )

        count = 0
        for sched in due_schedules:
            # Extract debounce_key from args
            args = ast.literal_eval(sched.args) if sched.args else ()
            if args:
                debounce_key = args[0]
                # Call the execute_debounced_task function
                from .tasks import execute_debounced_task
                execute_debounced_task(debounce_key)
                count += 1

            sched.delete()

        return count
