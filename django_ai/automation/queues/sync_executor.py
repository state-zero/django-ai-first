import time
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class DebouncedTaskInfo:
    """In-memory debounced task tracking."""
    scheduled_at: datetime
    task_name: str
    args: tuple
    event_ids: list[int] = field(default_factory=list)


class SynchronousExecutor:
    """Executes queued steps inline for integration tests."""

    def __init__(self):
        self._delayed_tasks: list[tuple[datetime, str, tuple]] = []
        # In-memory debounce tracking for sync executor
        self._debounced_tasks: dict[str, DebouncedTaskInfo] = {}

    def queue_task(self, task_name: str, *args, delay: Optional[timedelta] = None):
        """Execute tasks synchronously, or queue for later if delayed."""
        from django.utils import timezone

        if delay:
            run_at = timezone.now() + delay
            self._delayed_tasks.append((run_at, task_name, args))
            return

        self._execute_task(task_name, args)

    def debounce_task(
        self,
        debounce_key: str,
        task_name: str,
        *args,
        delay: timedelta,
        event_id: Optional[int] = None,
    ):
        """
        Schedule a task with debouncing (synchronous/in-memory version).

        If a task with the same debounce_key is already pending, its execution
        time is pushed forward by the delay. Only when no new calls come in
        for the full delay period will the task actually execute.

        Events are accumulated during the debounce window.
        """
        from django.utils import timezone

        scheduled_at = timezone.now() + delay

        if debounce_key in self._debounced_tasks:
            # Update existing - push time forward and accumulate event
            task_info = self._debounced_tasks[debounce_key]
            task_info.scheduled_at = scheduled_at
            task_info.task_name = task_name
            task_info.args = args
            if event_id and event_id not in task_info.event_ids:
                task_info.event_ids.append(event_id)
        else:
            # Create new
            self._debounced_tasks[debounce_key] = DebouncedTaskInfo(
                scheduled_at=scheduled_at,
                task_name=task_name,
                args=args,
                event_ids=[event_id] if event_id else [],
            )

    def process_delayed_tasks(self, now: datetime) -> int:
        """Process delayed tasks that are due. Returns count of tasks processed."""
        due = [(t, name, args) for t, name, args in self._delayed_tasks if t <= now]
        self._delayed_tasks = [(t, name, args) for t, name, args in self._delayed_tasks if t > now]

        for _, task_name, args in due:
            self._execute_task(task_name, args)

        count = len(due)

        # Also process debounced tasks
        count += self.process_debounced_tasks(now)

        return count

    def process_debounced_tasks(self, now: datetime) -> int:
        """Process debounced tasks that are due."""
        due_keys = [
            key for key, info in self._debounced_tasks.items()
            if info.scheduled_at <= now
        ]

        count = 0
        for key in due_keys:
            task_info = self._debounced_tasks.pop(key)
            self._execute_debounced_task(task_info)
            count += 1

        return count

    def _execute_debounced_task(self, task_info: DebouncedTaskInfo):
        """Execute a debounced task with its accumulated events."""
        from django_ai.automation.events.models import Event

        # Load events from IDs
        events = list(Event.objects.filter(id__in=task_info.event_ids))

        # Execute the task with events
        self._execute_task(task_info.task_name, task_info.args, events=events)

    def _execute_task(self, task_name: str, args: tuple, events: list = None):
        """Execute a task immediately.

        Args:
            task_name: Name of the task to execute
            args: Positional arguments for the task
            events: Optional list of Event objects (for debounced tasks)
        """
        from django_ai.automation.workflows.core import engine
        from django_ai.automation.events.services import event_processor

        if task_name == "execute_step":
            run_id, step_name = args
            engine.execute_step(run_id, step_name)
        elif task_name == "process_scheduled_workflows":
            engine.process_scheduled()
        elif task_name == "process_scheduled_agents":
            engine.process_scheduled()
        elif task_name == "poll_due_events":
            hours = args[0] if args else 24
            event_processor.process_due_events(lookback_hours=hours)
        elif task_name == "cleanup_old_events":
            days = args[0] if args else 30
            event_processor.cleanup_old_events(days_old=days)
        elif task_name == "process_conversation_message":
            message_id = args[0]
            from django_ai.conversations.tasks import process_conversation_message
            process_conversation_message(message_id)
        elif task_name == "execute_handler":
            execution_id = args[0]
            from django_ai.automation.agents.core import agent_engine
            agent_engine.execute_handler(execution_id)
        elif task_name == "execute_handler_for_events":
            # Debounced handler - passes events
            agent_run_id, handler_name = args
            from django_ai.automation.agents.core import agent_engine
            agent_engine.execute_handler_for_events(agent_run_id, handler_name, events=events or [])
        elif task_name == "start_workflow_for_events":
            # Debounced workflow - passes events
            workflow_name = args[0]
            engine.start_for_events(workflow_name, events=events or [])
        elif task_name == "execute_event_callback":
            # Debounced @on_event callback - passes events
            callback_key = args[0]
            from django_ai.automation.events.callbacks import callback_registry
            callback = callback_registry.get_callback_by_key(callback_key)
            if callback:
                callback(events or [])
        else:
            raise ValueError(f"Unsupported task {task_name} in SynchronousExecutor")
