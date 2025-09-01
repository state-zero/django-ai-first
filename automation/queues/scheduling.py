from django.conf import settings
from django_q.models import Schedule

def ensure_q2_schedules():
    if getattr(settings, "SKIP_Q2_AUTOSCHEDULE", False):
        return

    Schedule.objects.update_or_create(
        name="Workflows: process_scheduled",
        defaults=dict(
            func="pilot.automationqueues.tasks.process_scheduled_workflows",
            schedule_type=Schedule.MINUTES,
            minutes=1,
            repeats=-1,
        ),
    )

    Schedule.objects.update_or_create(
        name="Events: poll_due",
        defaults=dict(
            func="pilot.automationqueues.tasks.poll_due_events",
            schedule_type=Schedule.MINUTES,
            minutes=5,
            repeats=-1,
        ),
    )

    Schedule.objects.update_or_create(
        name="Events: cleanup_old",
        defaults=dict(
            func="pilot.automationqueues.tasks.cleanup_old_events",
            schedule_type=Schedule.DAILY,
            repeats=-1,
        ),
    )
