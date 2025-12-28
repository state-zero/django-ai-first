"""
Precise timers worker using asyncio with Sentinel supervision.

Architecture matches Django-Q2's cluster pattern:
- Sentinel (main process): monitors and reincarnates the worker
- Worker (subprocess): runs asyncio loop for precise timing

Usage:
    python manage.py run_timers

This should run as a single long-lived process.
"""

import asyncio
import logging
import signal
import time
from datetime import timedelta
from multiprocessing import Process, Event
from typing import Dict, Optional

from django.core.management.base import BaseCommand
from django.db import connections, transaction
from django.utils import timezone

from ...models import TimedTask, TaskStatus

logger = logging.getLogger(__name__)


def close_old_connections():
    """Close stale database connections."""
    for conn in connections.all():
        conn.close_if_unusable_or_obsolete()


class TimersSentinel:
    """
    Supervisor process that monitors and reincarnates the timer worker.

    Matches Django-Q2's Sentinel pattern but simplified for single worker.
    """

    def __init__(self, poll_interval: float = 0.1, guard_cycle: float = 1.0):
        self.poll_interval = poll_interval
        self.guard_cycle = guard_cycle
        self.stop_event = Event()
        self.worker: Optional[Process] = None

    def start(self):
        """Start the sentinel guard loop."""
        # Set up signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info("Starting timers sentinel...")
        self.guard()

    def _signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, stopping...")
        self.stop_event.set()

    def guard(self):
        """Main guard loop - monitor and reincarnate worker."""
        self.spawn_worker()

        while not self.stop_event.is_set():
            # Check worker health
            if not self.worker.is_alive():
                self.reincarnate()

            time.sleep(self.guard_cycle)

        # Graceful shutdown
        self.stop()

    def spawn_worker(self):
        """Spawn the timer worker subprocess."""
        logger.info("Spawning timer worker...")
        self.worker = Process(
            target=run_timer_worker,
            args=(self.stop_event, self.poll_interval),
            daemon=True,
        )
        self.worker.start()

    def reincarnate(self):
        """Respawn a dead worker with fresh connections."""
        logger.warning("Worker died, reincarnating...")

        # Close all DB connections before spawning (Django-Q2 pattern)
        connections.close_all()

        # Terminate if still hanging
        if self.worker and self.worker.is_alive():
            self.worker.terminate()
            self.worker.join(timeout=5)

        self.spawn_worker()

    def stop(self):
        """Graceful shutdown."""
        logger.info("Stopping timers sentinel...")
        self.stop_event.set()

        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=10)
            if self.worker.is_alive():
                self.worker.terminate()

        logger.info("Timers sentinel stopped.")


def run_timer_worker(stop_event: Event, poll_interval: float):
    """
    Timer worker subprocess entry point.

    Runs the asyncio event loop for precise timing.
    """
    # Set up Django in subprocess
    import django
    django.setup()

    logger.info("Timer worker started")

    try:
        asyncio.run(timer_loop(stop_event, poll_interval))
    except Exception as e:
        logger.error(f"Timer worker crashed: {e}", exc_info=True)
        raise
    finally:
        logger.info("Timer worker exiting")


async def timer_loop(stop_event: Event, poll_interval: float):
    """
    Main asyncio loop for precise task timing.
    """
    from django_ai.automation.queues.q2_executor import Q2Executor

    executor = Q2Executor()
    pending_tasks: Dict[int, asyncio.Task] = {}

    # Recover orphaned tasks on startup
    await recover_orphaned_tasks(executor)

    while not stop_event.is_set():
        # Close stale connections each cycle
        close_old_connections()

        # Check for new/updated tasks
        await check_for_tasks(executor, pending_tasks, stop_event)

        await asyncio.sleep(poll_interval)

    # Cancel pending asyncio tasks on shutdown
    for task in pending_tasks.values():
        task.cancel()


async def recover_orphaned_tasks(executor):
    """Dispatch any tasks that missed their run_at time."""
    now = timezone.now()
    orphaned = TimedTask.objects.filter(
        status=TaskStatus.PENDING,
        run_at__lt=now - timedelta(seconds=1),
    )

    count = 0
    for task in orphaned:
        await dispatch_task(task, executor)
        count += 1

    if count > 0:
        logger.info(f"Recovered {count} orphaned tasks")


async def check_for_tasks(executor, pending_tasks: Dict[int, asyncio.Task], stop_event: Event):
    """Check for pending tasks and schedule their execution."""
    now = timezone.now()

    pending = TimedTask.objects.filter(status=TaskStatus.PENDING)

    for task in pending:
        task_id = task.id

        # Skip if already scheduled
        if task_id in pending_tasks:
            existing = pending_tasks[task_id]
            if not existing.done():
                continue

        delay = (task.run_at - now).total_seconds()

        if delay <= 0:
            await dispatch_task(task, executor)
        else:
            asyncio_task = asyncio.create_task(
                wait_and_dispatch(task_id, delay, executor, pending_tasks)
            )
            pending_tasks[task_id] = asyncio_task


async def wait_and_dispatch(task_id: int, delay: float, executor, pending_tasks: dict):
    """Wait for delay, then dispatch if still due."""
    try:
        await asyncio.sleep(delay)

        try:
            task = TimedTask.objects.get(id=task_id)
        except TimedTask.DoesNotExist:
            return

        if task.status != TaskStatus.PENDING:
            return

        now = timezone.now()
        if task.run_at > now:
            # run_at was extended (debounce), reschedule
            new_delay = (task.run_at - now).total_seconds()
            if new_delay > 0:
                asyncio_task = asyncio.create_task(
                    wait_and_dispatch(task_id, new_delay, executor, pending_tasks)
                )
                pending_tasks[task_id] = asyncio_task
                return

        await dispatch_task(task, executor)

    finally:
        pending_tasks.pop(task_id, None)


async def dispatch_task(task: TimedTask, executor):
    """Dispatch the task to the executor."""
    with transaction.atomic():
        task = TimedTask.objects.select_for_update().get(id=task.id)

        if task.status != TaskStatus.PENDING:
            return

        if task.run_at > timezone.now():
            return

        task.status = TaskStatus.DISPATCHED
        task.dispatched_at = timezone.now()
        task.save(update_fields=["status", "dispatched_at", "updated_at"])

    # Build kwargs with collected_args if present
    task_kwargs = dict(task.task_kwargs)
    if task.collected_args:
        task_kwargs["collected_args"] = task.collected_args

    executor.queue_task(
        task.task_name,
        *task.task_args,
        **task_kwargs,
    )

    logger.info(
        f"Dispatched {task.task_name} "
        f"(debounce_key={task.debounce_key}, collected={len(task.collected_args)})"
    )


class Command(BaseCommand):
    help = "Run the precise timers worker (scheduling and debounce)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=0.1,
            help="How often to check for new tasks (seconds, default: 0.1)",
        )
        parser.add_argument(
            "--guard-cycle",
            type=float,
            default=1.0,
            help="How often sentinel checks worker health (seconds, default: 1.0)",
        )

    def handle(self, *args, **options):
        poll_interval = options["poll_interval"]
        guard_cycle = options["guard_cycle"]

        self.stdout.write(self.style.SUCCESS("Starting timers service..."))

        sentinel = TimersSentinel(
            poll_interval=poll_interval,
            guard_cycle=guard_cycle,
        )

        try:
            sentinel.start()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Interrupted"))

        self.stdout.write(self.style.SUCCESS("Timers service stopped."))
