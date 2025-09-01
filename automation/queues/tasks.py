from django.db import transaction
from django.utils import timezone
from automation.workflows.core import engine
from automation.events.services import event_processor

def execute_step(run_id: int, step_name: str) -> None:
    engine.execute_step(run_id, step_name)


def process_scheduled_workflows() -> dict:
    with transaction.atomic():
        engine.process_scheduled()
    return {"processed_at": timezone.now().isoformat()}


def poll_due_events(hours: int = 24) -> dict:
    return event_processor.process_due_events(lookback_hours=hours)


def cleanup_old_events(days: int = 30) -> dict:
    return event_processor.cleanup_old_events(days_old=days)
