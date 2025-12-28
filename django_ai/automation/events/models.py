from __future__ import annotations
from django.db import models
from django.db.models import (
    Q,
    Exists,
    OuterRef,
    Subquery,
    Case,
    When,
    Value,
    DateTimeField,
    F,
)
from django.db.models.functions import Cast
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from datetime import datetime
from typing import List, Optional
from .definitions import EventDefinition, EventTrigger
from django.db import transaction
import logging
from collections import defaultdict
from cytoolz import groupby, pipe
from collections import defaultdict

logger = logging.getLogger(__name__)


class EventStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSED = "processed", "Processed"  # Internal status - maps to OCCURRED callbacks
    CANCELLED = "cancelled", "Cancelled"


class EventManager(models.Manager):

    def get_queryset(self):
        return EventQuerySet(self.model, using=self._db)

    def _build_date_prefilter_q_and_due_annotation(
        self,
        from_date: Optional[datetime],
        to_date: Optional[datetime],
        include_immediate: bool,
    ):
        """
        Build a Q that trims events by date entirely in SQL, plus a due_at_db annotation:
        - scheduled events: entity.<date_field> in [from,to]
        - immediate events: event_created_at in [from,to] (when include_immediate)
        """
        scheduled_q = Q()
        immediate_q = Q()
        due_whens = []

        for model in apps.get_models():
            defs = getattr(model, "events", None)
            if not defs:
                continue
            ct = ContentType.objects.get_for_model(model)

            # Scheduled events
            for ed in defs:
                if ed.date_field:
                    # Build date range filter kwargs inline
                    date_kw = {}
                    if from_date:
                        date_kw[f"{ed.date_field}__gte"] = from_date
                    if to_date:
                        date_kw[f"{ed.date_field}__lte"] = to_date

                    # Cast entity_id (varchar) to match the model's PK type
                    entity_id_ref = Cast(OuterRef("entity_id"), output_field=model._meta.pk)

                    if date_kw:
                        ent_qs = model._default_manager.filter(
                            pk=entity_id_ref
                        ).filter(**date_kw)
                    else:
                        ent_qs = model._default_manager.filter(pk=entity_id_ref)
                    scheduled_q |= Q(model_type=ct, event_name=ed.name) & Exists(ent_qs)

                    due_subq = model._default_manager.filter(
                        pk=entity_id_ref
                    ).values(ed.date_field)[:1]
                    due_whens.append(
                        When(model_type=ct, event_name=ed.name, then=Subquery(due_subq))
                    )
                else:
                    # Immediate event
                    if include_immediate:
                        created_kw = {}
                        if from_date:
                            created_kw["event_created_at__gte"] = from_date
                        if to_date:
                            created_kw["event_created_at__lte"] = to_date
                        immediate_q |= Q(
                            model_type=ct, event_name=ed.name, **created_kw
                        )

        due_at = Case(
            *due_whens,
            default=F("event_created_at"),
            output_field=DateTimeField(),
        )

        date_q = scheduled_q | (immediate_q if include_immediate else Q())

        return date_q, {"due_at_db": due_at}

    def get_events(
        self,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        status: Optional[EventStatus] = None,
        include_immediate: bool = True,
    ) -> List[Event]:
        """
        Builds the query for events and uses the .with_entities() queryset
        method to efficiently fetch related data.
        """
        base = self.get_queryset().select_related("model_type")
        if status:
            base = base.filter(status=status)

        date_q, annotations = self._build_date_prefilter_q_and_due_annotation(
            from_date, to_date, include_immediate
        )

        # Build the query, then chain our custom method to execute it and hydrate the results.
        qs = base.filter(date_q).annotate(**annotations).order_by("due_at_db")

        # The call to .with_entities() now handles everything else.
        hydrated_events = qs.with_entities()

        # The final validation can now be done cleanly.
        results = []
        for ev in hydrated_events:
            if not hasattr(ev, "_entity_cache"):
                continue # Skip events whose entities were deleted

            if not include_immediate and ev.is_immediate:
                continue

            if ev.is_valid:
                results.append(ev)

        return results

    def get_due_events(
        self,
        from_date: datetime,
        to_date: datetime,
        status: Optional[EventStatus] = None,
    ) -> List[Event]:
        """Get events due within a specific date range (excludes immediate events)"""
        return self.get_events(from_date, to_date, status, include_immediate=False)

    def update_for_instance(
        self, instance: models.Model, trigger: EventTrigger = EventTrigger.CREATE
    ) -> None:
        """Create/update events for a specific model instance based on trigger type"""
        if not hasattr(instance.__class__, "events"):
            return

        content_type = ContentType.objects.get_for_model(instance.__class__)

        for event_def in instance.__class__.events:
            # Get current watched values hash (None if no watch_fields defined)
            current_watched = event_def.get_watched_values_hash(instance)

            # Render event name from template (supports {{ instance.field }} syntax)
            event_name = event_def.get_name(instance)

            # Always create event records (for tracking), but only fire based on trigger
            event, created = self.get_or_create(
                model_type=content_type,
                entity_id=str(instance.pk),
                event_name=event_name,
                namespace=event_def.get_namespace(instance)
            )

            # Update watched_values_hash snapshot on create
            if created and current_watched is not None:
                event.watched_values_hash = current_watched
                event.save(update_fields=["watched_values_hash"])

            # Skip firing if this event doesn't match the current trigger
            if not event_def.should_trigger(trigger):
                continue

            # For UPDATE/DELETE triggers, handle watch_fields and reset to PENDING
            if trigger in (EventTrigger.UPDATE, EventTrigger.DELETE) and not created:
                # If watch_fields defined, check if any watched values changed
                if current_watched is not None:
                    if event.watched_values_hash == current_watched:
                        # No change in watched fields, skip entirely
                        continue
                    # Values changed - update the snapshot
                    event.watched_values_hash = current_watched
                    event.save(update_fields=["watched_values_hash"])

                # Reset to PENDING if already processed, so it can fire again
                if event.status == EventStatus.PROCESSED:
                    event.status = EventStatus.PENDING
                    event.save(update_fields=["status"])

            # Fire immediate events right after the entity commit if valid & pending
            try:
                if event_def.is_immediate() and event.status == EventStatus.PENDING:
                    # Use the instance we already have to evaluate the condition cheaply
                    if event_def.should_create(instance):
                        event_id = event.pk

                        # For DELETE triggers, fire synchronously since entity will be gone after commit
                        if trigger == EventTrigger.DELETE:
                            event.mark_as_occurred()
                        else:
                            def _mark_after_commit():
                                try:
                                    # Use select_for_update to prevent race conditions
                                    ev = Event.objects.select_for_update().get(pk=event_id)
                                    # idempotent guard if something else processed it
                                    if ev.status == EventStatus.PENDING and ev.is_valid:
                                        ev.mark_as_occurred()  # triggers OCCURRED callbacks
                                except Event.DoesNotExist:
                                    pass
                                except Exception as e:
                                    logger.error(
                                        f"Failed to process immediate event {event_id}: {e}"
                                    )

                            transaction.on_commit(_mark_after_commit)
            except Exception as e:
                # Be conservative: never block the save flow because of callback firing
                logger.warning(f"Error validating event {event.id}: {e}")


class Event(models.Model):
    event_name: str = models.CharField(max_length=100)
    status: EventStatus = models.CharField(
        max_length=20, choices=EventStatus.choices, default=EventStatus.PENDING
    )

    # Generic foreign key to any model
    model_type: ContentType = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    entity_id: str = models.CharField(max_length=50)

    event_created_at: datetime = models.DateTimeField(auto_now_add=True)
    event_updated_at: datetime = models.DateTimeField(auto_now=True)

    watched_values_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="SHA256 hash of watched field values for change detection",
    )

    namespace = models.CharField(
        max_length=255,
        default="*",
        help_text="Namespace to scope events and prevent cross-application triggering",
    )

    objects: EventManager = EventManager()

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["model_type", "entity_id"]),
        ]
        unique_together = ["model_type", "entity_id", "event_name", "namespace"]

    def __str__(self) -> str:
        return f"{self.event_name} at {self.at} for {self.model_type.model} {self.entity_id}"

    def save(self, *args, **kwargs):
        """Override save to trigger callbacks on status changes"""
        from .callbacks import callback_registry, EventPhase

        # Track if this is a new event or status change
        is_new = self._state.adding
        old_status = None

        if not is_new:
            old_event = Event.objects.filter(pk=self.pk).only("status").first()
            if old_event:
                old_status = old_event.status                

        # Save the event first
        super().save(*args, **kwargs)

        # Trigger callbacks after successful save
        if is_new:
            callback_registry.notify(self, EventPhase.CREATED)
        elif old_status != self.status:
            if self.status == EventStatus.PROCESSED:
                callback_registry.notify(self, EventPhase.OCCURRED)
            elif self.status == EventStatus.CANCELLED:
                callback_registry.notify(self, EventPhase.CANCELLED)

    def mark_as_occurred(self):
        """Mark this event as having occurred - triggers OCCURRED callbacks"""
        if self.status == EventStatus.PENDING:
            self.status = EventStatus.PROCESSED
            self.save()

    def cancel(self):
        """Cancel this event - triggers CANCELLED callbacks"""
        if self.status == EventStatus.PENDING:
            self.status = EventStatus.CANCELLED
            self.save()

    @property
    def at(self) -> Optional[datetime]:
        """When this event occurs"""
        try:
            event_def = self._get_event_definition()
            if not event_def:
                return None

            if event_def.is_immediate():
                return self.event_created_at
            else:
                entity = self.entity
                return event_def.get_date_value(entity)
        except Exception:
            return None

    @property
    def is_immediate(self) -> bool:
        """Check if this is an immediate event"""
        try:
            event_def = self._get_event_definition()
            return event_def.is_immediate() if event_def else False
        except Exception:
            return False

    @property
    def entity(self) -> models.Model:
        if hasattr(self, "_entity_cache"):
            return self._entity_cache
        model_class = self.model_type.model_class()
        return model_class.objects.get(pk=self.entity_id)

    @property
    def is_valid(self) -> bool:
        """Check if this event is currently valid based on its condition"""
        try:
            entity = self.entity
            event_def = self._get_event_definition()
            return event_def.should_create(entity) if event_def else False
        except Exception:
            return False

    def _get_event_definition(self) -> Optional[EventDefinition]:
        """Find the EventDefinition that created this event"""
        try:
            entity = self.entity
            if hasattr(entity.__class__, "events"):
                for event_def in entity.__class__.events:
                    # Compare rendered name (handles templates like "task:{{ instance.pk }}")
                    if event_def.get_name(entity) == self.event_name:
                        return event_def
        except Exception:
            pass
        return None


class ProcessedEventOffset(models.Model):
    """
    Tracks which (event, offset) combinations have had their callbacks fired.

    Once an offset callback has fired for an event, it won't fire again even
    if the event moves (date changes). This prevents duplicate side effects.
    """
    event = models.ForeignKey(
        'Event',
        on_delete=models.CASCADE,
        related_name='processed_offsets'
    )
    offset = models.DurationField()
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['event', 'offset']
        indexes = [
            models.Index(fields=['event', 'offset']),
        ]

    def __str__(self):
        return f"Processed {self.event.event_name} @ offset {self.offset}"


class EventClaim(models.Model):
    """
    Tracks which flow (agent/workflow) has claimed an event type.

    Claims use Django ORM filter syntax for matching - when an event fires,
    the match dict is applied to the event's entity to determine if the claim
    applies. Only the claim holder's handlers receive matching events
    (unless handlers have ignores_claims=True).

    Example:
        EventClaim(
            event_type="message_received",
            match={"guest_id": 123},  # Django filter syntax
            owner_type="agent",
            owner_class="SupportFlow",
            owner_run_id=456,
        )
    """
    event_type = models.CharField(max_length=100)
    match = models.JSONField()  # Django ORM filter syntax, e.g. {"guest_id": 123}

    # Owner identification
    owner_type = models.CharField(max_length=50)   # "agent" or "workflow"
    owner_class = models.CharField(max_length=255)
    owner_run_id = models.BigIntegerField()

    # Claim properties
    priority = models.IntegerField(default=0)

    # Expiry - time-based
    expires_at = models.DateTimeField(null=True, blank=True)

    # Expiry - event-count-based
    max_events = models.IntegerField(null=True, blank=True)
    events_handled = models.IntegerField(default=0)

    # Tracking
    claimed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["event_type", "expires_at"]),
            models.Index(fields=["owner_type", "owner_run_id"]),
        ]

    def __str__(self):
        return f"{self.owner_class} claims {self.event_type} (match={self.match})"

    @property
    def is_expired(self):
        from django.utils import timezone
        if self.expires_at and timezone.now() > self.expires_at:
            return True
        if self.max_events is not None and self.events_handled >= self.max_events:
            return True
        return False

    def increment_events(self):
        """Call after successfully handling a claimed event."""
        self.events_handled = models.F('events_handled') + 1
        self.save(update_fields=["events_handled"])
        self.refresh_from_db(fields=["events_handled"])


class EventQuerySet(models.QuerySet):

    def with_entities(self):
        """Use cytoolz for memory-efficient processing"""
        # Use iterator() instead of list() - stays lazy
        events_iter = self.iterator()

        # cytoolz groupby works with iterators efficiently
        events_by_type = groupby(lambda ev: ev.model_type, events_iter)

        entity_cache = {}
        all_events = []

        # Process each content type group
        for content_type, events_group in events_by_type.items():
            events_list = list(events_group)  # Only materialize per-type
            all_events.extend(events_list)

            # Get PKs for this type
            pks = {ev.entity_id for ev in events_list}

            # Bulk fetch entities
            model_class = content_type.model_class()
            fetched_entities = model_class.objects.filter(pk__in=pks)

            # Cache entities
            for entity in fetched_entities:
                entity_cache[(content_type.id, str(entity.pk))] = entity

        # Attach cached entities
        for ev in all_events:
            entity = entity_cache.get((ev.model_type_id, ev.entity_id))
            if entity:
                ev._entity_cache = entity

        return all_events
