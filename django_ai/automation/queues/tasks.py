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


def wake_workflow(run_id: int, expected_wake_at: str) -> None:
    """
    Wake a sleeping workflow. Called by timers for precise wake-up.

    Args:
        run_id: The workflow run ID
        expected_wake_at: ISO format datetime - must match current wake_at to proceed.
            This prevents stale timers from waking a workflow that has since
            gone through another sleep cycle.

    The workflow must be in WAITING status with matching wake_at.
    This is a belt-and-suspenders approach: timers provide precision,
    but wake_at fallback ensures process_scheduled catches it if timer fails.
    """
    from django.utils.dateparse import parse_datetime
    from ..workflows.models import WorkflowRun, WorkflowStatus

    expected_dt = parse_datetime(expected_wake_at)

    with transaction.atomic():
        try:
            run = WorkflowRun.objects.select_for_update(skip_locked=True).get(
                id=run_id,
                status=WorkflowStatus.WAITING,
            )
        except WorkflowRun.DoesNotExist:
            # Already woken by process_scheduled or status changed
            return

        # Check if this timer is stale (workflow has slept again with different wake_at)
        if run.wake_at != expected_dt:
            return

        run.status = WorkflowStatus.RUNNING
        run.wake_at = None
        run.save()

    # Queue the step execution outside the transaction
    engine._queue_step(run_id, run.current_step)
