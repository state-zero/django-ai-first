"""
Cache-driven precise scheduling and debounce API.

Pending tasks live in the cache (no DB locking issues).
The database only stores executed tasks for audit/history.

Usage:
    from django_ai.automation.timers import schedule_task, debounce_task

    # One-shot precise scheduling
    schedule_task(
        "myapp.tasks.send_email",
        args=[user_id, "Hello"],
        run_at=timezone.now() + timedelta(seconds=30),
    )

    # Debounced task - extends window on each call, collects args
    debounce_task(
        "myapp.tasks.process_updates",
        debounce_key=f"user:{user_id}:updates",
        debounce_window=timedelta(milliseconds=500),
        collect=event_id,  # Accumulated in collected_args
    )

Configuration:
    Set DJANGO_AI['TIMERS_IN_PROCESS'] in your Django settings:
    - True: Execute tasks in-process via background thread (for local dev)
    - False: Require 'python manage.py run_timers' worker (for production)
"""

import importlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, List, Dict, Optional

from django.core.cache import cache
from django.utils import timezone

from .models import TimedTask

logger = logging.getLogger(__name__)

# Cache key patterns
REGISTRY_KEY = "timers:registry"
REGISTRY_LOCK_KEY = "timers:registry_lock"
TASK_DATA_KEY = "timers:data:{task_id}"
TASK_LOCK_KEY = "timers:lock:{task_id}"

# Background thread state
_sync_thread: Optional[threading.Thread] = None
_sync_thread_stop = threading.Event()
_sync_thread_paused = threading.Event()  # Set when time_machine is active
_sync_thread_lock = threading.Lock()


@dataclass
class ProcessResult:
    """Result of processing due tasks."""
    dispatched: int = 0
    errors: int = 0


@dataclass
class PendingTask:
    """In-memory representation of a pending task."""
    task_id: str
    task_name: str
    task_args: List[Any] = field(default_factory=list)
    task_kwargs: Dict[str, Any] = field(default_factory=dict)
    collected_args: List[Any] = field(default_factory=list)
    run_at: Any = None  # datetime
    debounce_key: Optional[str] = None
    is_debounce: bool = False

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "task_args": self.task_args,
            "task_kwargs": self.task_kwargs,
            "collected_args": self.collected_args,
            "run_at": self.run_at.isoformat() if self.run_at else None,
            "debounce_key": self.debounce_key,
            "is_debounce": self.is_debounce,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingTask":
        from django.utils.dateparse import parse_datetime
        run_at = data.get("run_at")
        if run_at and isinstance(run_at, str):
            run_at = parse_datetime(run_at)
        return cls(
            task_id=data["task_id"],
            task_name=data["task_name"],
            task_args=data.get("task_args", []),
            task_kwargs=data.get("task_kwargs", {}),
            collected_args=data.get("collected_args", []),
            run_at=run_at,
            debounce_key=data.get("debounce_key"),
            is_debounce=data.get("is_debounce", False),
        )


def _atomic_registry_update(task_id: str, remove: bool = False) -> bool:
    """
    Safely update the task registry using a spin-lock on the cache.

    Returns True if successful, False if lock couldn't be acquired.
    """
    # Try to acquire lock for up to 5 seconds
    for _ in range(50):
        if cache.add(REGISTRY_LOCK_KEY, "1", timeout=5):
            try:
                registry = cache.get(REGISTRY_KEY) or set()
                if remove:
                    registry.discard(task_id)
                else:
                    registry.add(task_id)
                cache.set(REGISTRY_KEY, registry, timeout=None)
                return True
            finally:
                cache.delete(REGISTRY_LOCK_KEY)
        time.sleep(0.1)
    return False


def _store_task(task: PendingTask) -> None:
    """Store task data in cache and register it."""
    cache_key = TASK_DATA_KEY.format(task_id=task.task_id)
    cache.set(cache_key, task.to_dict(), timeout=None)
    _atomic_registry_update(task.task_id)


def _get_task(task_id: str) -> Optional[PendingTask]:
    """Retrieve task data from cache."""
    cache_key = TASK_DATA_KEY.format(task_id=task_id)
    data = cache.get(cache_key)
    if data:
        return PendingTask.from_dict(data)
    return None


def _delete_task(task_id: str) -> None:
    """Remove task from cache and registry."""
    cache_key = TASK_DATA_KEY.format(task_id=task_id)
    cache.delete(cache_key)
    _atomic_registry_update(task_id, remove=True)


def schedule_task(
    task_name: str,
    args: Optional[List[Any]] = None,
    kwargs: Optional[Dict[str, Any]] = None,
    run_at: Optional[timezone.datetime] = None,
    delay: Optional[timedelta] = None,
) -> str:
    """
    Schedule a task for precise one-shot execution.

    Args:
        task_name: Dotted path to the task function (e.g., "myapp.tasks.send_email")
        args: Positional arguments to pass to the task
        kwargs: Keyword arguments to pass to the task
        run_at: Exact datetime to execute (mutually exclusive with delay)
        delay: Timedelta from now to execute (mutually exclusive with run_at)

    Returns:
        The task ID (string)
    """
    if run_at is None and delay is None:
        raise ValueError("Either run_at or delay must be specified")
    if run_at is not None and delay is not None:
        raise ValueError("Cannot specify both run_at and delay")

    if delay is not None:
        run_at = timezone.now() + delay

    # Check in-process mode setting (triggers warning if not explicitly set)
    from django_ai.conf import is_timers_in_process_mode
    is_timers_in_process_mode()

    task_id = f"once:{uuid.uuid4().hex[:12]}"
    task = PendingTask(
        task_id=task_id,
        task_name=task_name,
        task_args=args or [],
        task_kwargs=kwargs or {},
        run_at=run_at,
    )
    _store_task(task)

    return task_id


def debounce_task(
    task_name: str,
    debounce_key: str,
    debounce_window: timedelta,
    args: Optional[List[Any]] = None,
    kwargs: Optional[Dict[str, Any]] = None,
    collect: Optional[Any] = None,
) -> str:
    """
    Schedule a debounced task. Repeated calls with the same debounce_key
    extend the window and optionally collect arguments.

    Args:
        task_name: Dotted path to the task function
        debounce_key: Unique key to group debounced calls
        debounce_window: How long to wait after the last call before executing
        args: Positional arguments (set on first call, ignored on subsequent)
        kwargs: Keyword arguments (set on first call, ignored on subsequent)
        collect: Value to append to collected_args (optional)

    Returns:
        The task ID (string)

    Example:
        # Each call extends the window and collects the event_id
        debounce_task(
            "myapp.tasks.process_events",
            debounce_key=f"user:{user_id}",
            debounce_window=timedelta(seconds=1),
            collect=event_id,
        )

        # When finally executed, task receives collected_args:
        # process_events(*args, **kwargs, collected_args=[evt1, evt2, evt3])
    """
    task_id = f"debounce:{debounce_key}"
    run_at = timezone.now() + debounce_window

    cache_key = TASK_DATA_KEY.format(task_id=task_id)

    # Load existing task or create new
    existing_data = cache.get(cache_key)

    if existing_data:
        task = PendingTask.from_dict(existing_data)
        # Extend the window
        task.run_at = run_at
        # Collect the argument
        if collect is not None:
            task.collected_args = task.collected_args + [collect]
    else:
        task = PendingTask(
            task_id=task_id,
            task_name=task_name,
            task_args=args or [],
            task_kwargs=kwargs or {},
            collected_args=[collect] if collect is not None else [],
            run_at=run_at,
            debounce_key=debounce_key,
            is_debounce=True,
        )

    _store_task(task)

    # Note: We still check the setting to trigger the warning if not set.
    from django_ai.conf import is_timers_in_process_mode
    is_timers_in_process_mode()  # Trigger warning if not set

    return task_id


def _import_task(task_name: str):
    """Import and return a task function from its dotted path."""
    module_path, func_name = task_name.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


def _execute_task(task: PendingTask) -> tuple[bool, Optional[str]]:
    """
    Execute a task and return (success, error_message).
    """
    try:
        func = _import_task(task.task_name)
        kwargs = dict(task.task_kwargs)

        # For debounced tasks, pass collected_args
        if task.is_debounce and task.collected_args:
            kwargs["collected_args"] = task.collected_args

        func(*task.task_args, **kwargs)
        return True, None
    except Exception as e:
        logger.exception("Task execution failed: %s", task.task_name)
        return False, str(e)


def process_due_tasks() -> ProcessResult:
    """
    Process all tasks that are due for execution.

    Finds pending tasks in cache where run_at <= now, executes them,
    and logs results to the database.

    Returns:
        ProcessResult with counts of dispatched and errored tasks
    """
    result = ProcessResult()
    registry = cache.get(REGISTRY_KEY) or set()

    if not registry:
        return result

    now = timezone.now()

    for task_id in list(registry):
        lock_key = TASK_LOCK_KEY.format(task_id=task_id)

        # Atomic claim: only one worker can process this task
        if not cache.add(lock_key, "working", timeout=60):
            continue

        try:
            task = _get_task(task_id)
            if not task:
                # Task was deleted, clean up registry
                _atomic_registry_update(task_id, remove=True)
                continue

            # Check if task is due
            if task.run_at and task.run_at <= now:
                # Execute the task
                success, error_msg = _execute_task(task)

                # Log to database (single INSERT, no locks)
                TimedTask.objects.create(
                    task_name=task.task_name,
                    task_args=task.task_args,
                    task_kwargs=task.task_kwargs,
                    collected_args=task.collected_args,
                    debounce_key=task.debounce_key,
                    run_at=task.run_at,
                    success=success,
                    error_log=error_msg,
                )

                # Clean up cache
                _delete_task(task_id)

                if success:
                    result.dispatched += 1
                else:
                    result.errors += 1
        finally:
            cache.delete(lock_key)

    return result


# =============================================================================
# Sync mode background thread
# =============================================================================

def _sync_thread_loop(poll_interval: float = 0.1):
    """
    Background thread loop that polls cache for due tasks.

    Args:
        poll_interval: How often to check for due tasks (seconds)
    """
    logger.info("Timers sync thread started (poll_interval=%.2fs)", poll_interval)

    while not _sync_thread_stop.is_set():
        # Skip processing if paused (e.g., time_machine is active)
        if _sync_thread_paused.is_set():
            time.sleep(poll_interval)
            continue

        try:
            result = process_due_tasks()
            if result.dispatched > 0:
                logger.debug("Sync thread dispatched %d tasks", result.dispatched)
        except Exception as e:
            logger.error("Sync thread error: %s", e)

        time.sleep(poll_interval)

    logger.info("Timers sync thread stopped")


def start_sync_thread(poll_interval: float = 0.1) -> bool:
    """
    Start the background sync thread for TIMERS_SYNC mode.

    Returns:
        True if thread was started, False if already running
    """
    global _sync_thread

    with _sync_thread_lock:
        if _sync_thread is not None and _sync_thread.is_alive():
            return False

        _sync_thread_stop.clear()
        _sync_thread_paused.clear()
        _sync_thread = threading.Thread(
            target=_sync_thread_loop,
            args=(poll_interval,),
            daemon=True,  # Don't block process exit
            name="timers-sync",
        )
        _sync_thread.start()
        return True


def stop_sync_thread(timeout: float = 2.0) -> bool:
    """
    Stop the background sync thread.

    Returns:
        True if thread was stopped, False if not running
    """
    global _sync_thread

    with _sync_thread_lock:
        if _sync_thread is None or not _sync_thread.is_alive():
            return False

        _sync_thread_stop.set()
        _sync_thread.join(timeout=timeout)
        _sync_thread = None
        return True


def pause_sync_thread():
    """Pause the sync thread (called by time_machine on enter)."""
    _sync_thread_paused.set()


def resume_sync_thread():
    """Resume the sync thread (called by time_machine on exit)."""
    _sync_thread_paused.clear()


def is_sync_thread_running() -> bool:
    """Check if the sync thread is currently running."""
    return _sync_thread is not None and _sync_thread.is_alive()


# =============================================================================
# Cache utilities for testing
# =============================================================================

def clear_pending_tasks():
    """Clear all pending tasks from cache. Useful for testing."""
    registry = cache.get(REGISTRY_KEY) or set()
    for task_id in list(registry):
        cache_key = TASK_DATA_KEY.format(task_id=task_id)
        cache.delete(cache_key)
    cache.delete(REGISTRY_KEY)


def get_pending_task_count() -> int:
    """Get count of pending tasks in cache."""
    registry = cache.get(REGISTRY_KEY) or set()
    return len(registry)
