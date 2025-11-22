from django.core.management.base import BaseCommand
from django_ai.automation.queues.scheduling import ensure_q2_schedules


class Command(BaseCommand):
    help = "Set up Django-Q2 schedules for workflows, agents, and events"

    def handle(self, *args, **options):
        self.stdout.write("Setting up Django-Q2 schedules...")

        try:
            ensure_q2_schedules()
            self.stdout.write(self.style.SUCCESS("Successfully set up schedules"))
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"Failed to set up schedules: {e}")
            )
            raise
