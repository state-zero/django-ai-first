from django.core.management.base import BaseCommand
from django_ai.automation.events.services import event_processor


class Command(BaseCommand):
    help = "Process due events and mark them as occurred"

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours", type=int, default=24, help="Process events from the last N hours"
        )
        parser.add_argument(
            "--cleanup", action="store_true", help="Also cleanup old events"
        )
        parser.add_argument(
            "--cleanup-days",
            type=int,
            default=30,
            help="Delete events older than N days",
        )

    def handle(self, *args, **options):
        hours = options["hours"]

        # Process due events
        self.stdout.write("Processing due events...")
        results = event_processor.process_due_events(lookback_hours=hours)

        self.stdout.write(
            self.style.SUCCESS(
                f"Found {results['due_events_found']} due events, "
                f"processed {results['events_processed']} successfully"
            )
        )

        # Optional cleanup
        if options["cleanup"]:
            self.stdout.write("Cleaning up old events...")
            cleanup_results = event_processor.cleanup_old_events(
                days_old=options["cleanup_days"]
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Deleted {cleanup_results['deleted_count']} old events"
                )
            )
