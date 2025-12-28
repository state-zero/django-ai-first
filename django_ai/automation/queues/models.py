import json
from django.db import models
from django.utils import timezone


class DebouncedTask(models.Model):
    """
    Tracks pending debounced tasks.

    When a task is debounced with a key, we create/update a record here.
    When the delay expires, the task is executed and the record deleted.
    If a new task with the same key comes in before execution, we update
    the scheduled_at time (resetting the debounce window) and accumulate
    the event IDs.

    When executed, handlers/workflows receive all collected events via
    the `events` kwarg (list of Event objects).
    """
    debounce_key = models.CharField(max_length=255, unique=True, db_index=True)
    task_name = models.CharField(max_length=255)
    task_args_json = models.TextField(default="[]")
    # Accumulated event IDs during debounce window
    event_ids_json = models.TextField(default="[]")
    scheduled_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["scheduled_at"]),
        ]

    @property
    def task_args(self) -> tuple:
        return tuple(json.loads(self.task_args_json))

    @task_args.setter
    def task_args(self, value: tuple):
        self.task_args_json = json.dumps(list(value))

    @property
    def event_ids(self) -> list[int]:
        return json.loads(self.event_ids_json)

    @event_ids.setter
    def event_ids(self, value: list[int]):
        self.event_ids_json = json.dumps(value)

    def add_event_id(self, event_id: int):
        """Append an event ID to the accumulated list."""
        ids = self.event_ids
        if event_id not in ids:
            ids.append(event_id)
            self.event_ids = ids

    def __str__(self):
        return f"DebouncedTask({self.debounce_key}, {self.task_name}, events={len(self.event_ids)}, at={self.scheduled_at})"