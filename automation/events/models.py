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
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from datetime import datetime
from typing import List, Optional
from .definitions import EventDefinition
from django.db import transaction
import logging
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

                    if date_kw:
                        ent_qs = model._default_manager.filter(
                            pk=OuterRef("entity_id")
                        ).filter(**date_kw)
                    else:
                        ent_qs = model._default_manager.filter(pk=OuterRef("entity_id"))
                    scheduled_q |= Q(model_type=ct, event_name=ed.name) & Exists(ent_qs)

                    due_subq = model._default_manager.filter(
                        pk=OuterRef("entity_id")
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

    def update_for_instance(self, instance: models.Model) -> None:
        """Create events for a specific model instance"""
        if not hasattr(instance.__class__, "events"):
            return

        content_type = ContentType.objects.get_for_model(instance.__class__)

        for event_def in instance.__class__.events:
            event, _created = self.get_or_create(
                model_type=content_type,
                entity_id=str(instance.pk),
                event_name=event_def.name,
            )

            # Fire immediate events right after the entity commit if valid & pending
            try:
                if event_def.is_immediate() and event.status == EventStatus.PENDING:
                    # Use the instance we already have to evaluate the condition cheaply
                    if event_def.should_create(instance):
                        event_id = event.pk

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

    objects: EventManager = EventManager()

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["model_type", "entity_id"]),
        ]
        unique_together = ["model_type", "entity_id", "event_name"]

    def __str__(self) -> str:
        return f"{self.event_name} at {self.at} for {self.model_type.model} {self.entity_id}"

    def save(self, *args, **kwargs):
        """Override save to trigger callbacks on status changes"""
        from .callbacks import callback_registry, EventPhase

        # Track if this is a new event or status change
        is_new = self.pk is None
        old_status = None

        if not is_new:
            try:
                old_event = Event.objects.get(pk=self.pk)
                old_status = old_event.status
            except Event.DoesNotExist:
                is_new = True

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

    def _get_event_definition(self) -> Optional["EventDefinition"]:
        """Find the EventDefinition that created this event"""
        try:
            entity = self.entity
            if hasattr(entity.__class__, "events"):
                for event_def in entity.__class__.events:
                    if event_def.name == self.event_name:
                        return event_def
        except Exception:
            pass
        return None


class EventQuerySet(models.QuerySet):
    def with_entities(self):
        """
        Efficiently fetches related entities for the events in the queryset.

        This executes the primary query and then performs bulk queries to
        pre-fetch and attach the related entity for each event, preventing
        N+1 problems.
        """
        # The queryset (self) is still lazy at this point.
        # Evaluating it turns it into a list of candidate events.
        candidate_events = list(self)

        if not candidate_events:
            return []

        # Group entity PKs by their model type
        entities_to_fetch = defaultdict(set)
        for ev in candidate_events:
            entities_to_fetch[ev.model_type].add(ev.entity_id)

        # Run one bulk query per model type
        entity_cache = {}
        for content_type, pks in entities_to_fetch.items():
            model_class = content_type.model_class()
            fetched_entities = model_class.objects.filter(pk__in=pks)
            for entity in fetched_entities:
                entity_cache[(content_type.id, str(entity.pk))] = entity

        # Attach the fetched entities back to the event instances
        for ev in candidate_events:
            entity = entity_cache.get((ev.model_type_id, ev.entity_id))
            if entity:
                ev._entity_cache = entity

        return candidate_events
