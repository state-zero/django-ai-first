from django.db import transaction
from django.utils import timezone
from ..workflows.core import engine
from ..events.services import event_processor
from ...conversations.tasks import process_conversation_message
from ...files.tasks import process_file_ocr

def execute_step(run_id: int, step_name: str) -> None:
    engine.execute_step(run_id, step_name)


def process_scheduled_workflows() -> dict:
    with transaction.atomic():
        engine.process_scheduled()
    return {"processed_at": timezone.now().isoformat()}


def process_scheduled_agents() -> dict:
    from ..agents.core import agent_engine
    with transaction.atomic():
        agent_engine.process_scheduled_handlers()
    return {"processed_at": timezone.now().isoformat(), "kind": "agents"}


def poll_due_events(hours: int = 24) -> dict:
    return event_processor.process_due_events(lookback_hours=hours)


def cleanup_old_events(days: int = 30) -> dict:
    return event_processor.cleanup_old_events(days_old=days)


def execute_handler(execution_id: int) -> None:
    from ..agents.core import agent_engine
    agent_engine.execute_handler(execution_id)


def execute_debounced_task(debounce_key: str) -> None:
    """
    Execute a debounced task if it's due.

    This function is called when a debounce delay expires. It checks if
    the task should still run (i.e., hasn't been debounced again with
    a later scheduled_at time).

    Events accumulated during the debounce window are loaded and passed
    to the task via the `events` parameter.
    """
    from .models import DebouncedTask
    from ..events.models import Event

    with transaction.atomic():
        try:
            task = DebouncedTask.objects.select_for_update().get(
                debounce_key=debounce_key
            )
        except DebouncedTask.DoesNotExist:
            # Task was already executed or cancelled
            return

        # Check if the task is due (scheduled_at <= now)
        # If it's in the future, the task was debounced again
        if task.scheduled_at > timezone.now():
            # Task was debounced - a newer scheduled time exists
            # A new schedule will handle this
            return

        # Task is due - extract data before deleting
        task_name = task.task_name
        task_args = task.task_args
        event_ids = task.event_ids

        # Delete the record before executing to prevent re-execution
        task.delete()

    # Load events outside of transaction
    events = list(Event.objects.filter(id__in=event_ids))

    # Execute with events
    _execute_debounced_task_by_name(task_name, task_args, events=events)


def _execute_debounced_task_by_name(task_name: str, args: tuple, events: list = None) -> None:
    """Execute a task by name with the given args and accumulated events."""
    events = events or []

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
        process_conversation_message(message_id)
    elif task_name == "execute_handler":
        execution_id = args[0]
        from ..agents.core import agent_engine
        agent_engine.execute_handler(execution_id)
    elif task_name == "execute_handler_for_events":
        # Handler execution with accumulated events
        agent_run_id, handler_name = args
        from ..agents.core import agent_engine
        agent_engine.execute_handler_for_events(agent_run_id, handler_name, events=events)
    elif task_name == "start_workflow_for_events":
        # Workflow start with accumulated events
        workflow_name = args[0]
        engine.start_for_events(workflow_name, events=events)
    elif task_name == "execute_event_callback":
        # Debounced @on_event callback with accumulated events
        callback_key = args[0]
        from ..events.callbacks import callback_registry
        callback = callback_registry.get_callback_by_key(callback_key)
        if callback:
            callback(events)
    else:
        raise ValueError(f"Unsupported debounced task: {task_name}")
