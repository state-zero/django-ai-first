import time
from datetime import timedelta
from typing import Optional

class SynchronousExecutor:
    """Executes queued steps inline for integration tests."""

    def queue_task(self, task_name: str, *args, delay: Optional[timedelta] = None):
        """Execute tasks synchronously, respecting delays for testing accuracy"""

        from django_ai.automation.workflows.core import engine
        from django_ai.automation.events.services import event_processor

        if delay:
            time.sleep(delay.total_seconds())

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
        else:
            raise ValueError(f"Unsupported task {task_name} in SynchronousExecutor")
