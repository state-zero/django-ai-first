"""
Event claims system for controlling which flow handles events.

Claims use Django ORM filter syntax for matching - when you claim an event type
with a match dict, only your handlers receive events where the entity matches
that filter.

Usage in handlers:

    from django_ai.automation.events import claims

    @handler("ticket_created")
    def on_spawn(self):
        # Claim events matching this filter
        claims.hold("message_received", match={"guest_id": self.context.guest_id})

        # With custom expiry
        claims.hold("message_received", match={"guest_id": self.context.guest_id},
                    expires_in=timedelta(hours=2))

        # With event count limit
        claims.hold("message_received", match={"guest_id": self.context.guest_id},
                    expires_after_events=5)

    @handler("ticket_resolved")
    def cleanup(self):
        claims.release("message_received", match={"guest_id": self.context.guest_id})

For handlers that should always run regardless of claims:

    @handler("message_received", ignores_claims=True)
    def audit_all_messages(self):
        pass
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, TYPE_CHECKING

from django.apps import apps
from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from django.db.models import Model


DEFAULT_EXPIRY = timedelta(minutes=60)


@dataclass
class ClaimOwner:
    """Identifies who owns a claim."""
    owner_type: str  # "agent" or "workflow"
    owner_class: type
    owner_run_id: int


@dataclass
class ClaimInfo:
    """Information about a claim, returned by holder()."""
    event_type: str
    match: dict
    owner_type: str
    owner_class: str
    owner_run_id: int
    priority: int
    expires_at: Optional["timezone.datetime"]
    max_events: Optional[int]
    events_handled: int
    claimed_at: "timezone.datetime"

    @property
    def is_expired(self) -> bool:
        if self.expires_at and timezone.now() > self.expires_at:
            return True
        if self.max_events is not None and self.events_handled >= self.max_events:
            return True
        return False


# Context variable to track current execution context
_current_owner: ContextVar[Optional[ClaimOwner]] = ContextVar("claim_owner", default=None)


@contextmanager
def _context(
    owner_type: str,
    owner_class: type,
    owner_run_id: int,
):
    """
    Context manager to set the current claim owner.

    Used by agent/workflow engines when executing handlers/steps.
    """
    owner = ClaimOwner(
        owner_type=owner_type,
        owner_class=owner_class,
        owner_run_id=owner_run_id,
    )
    token = _current_owner.set(owner)
    try:
        yield owner
    finally:
        _current_owner.reset(token)


def _get_current_owner() -> ClaimOwner:
    """Get current owner or raise if not in execution context."""
    owner = _current_owner.get()
    if owner is None:
        raise RuntimeError(
            "claims.hold/release must be called from within a handler or workflow step. "
            "No execution context is active."
        )
    return owner


def _get_model_for_event_type(event_type: str) -> Optional[type]:
    """
    Find the model class that defines this event type.

    Searches all models for one with an events list containing this event_type.
    """
    for model in apps.get_models():
        events = getattr(model, "events", None)
        if events:
            for event_def in events:
                if event_def.name == event_type:
                    return model
    return None


def _validate_match(event_type: str, match: dict) -> None:
    """
    Validate that match dict is valid ORM filter syntax.

    Runs the filter against the model to catch typos and invalid fields early.

    Raises:
        ValueError: If match contains invalid filter syntax
    """
    model = _get_model_for_event_type(event_type)
    if model is None:
        raise ValueError(f"Unknown event type: {event_type}")

    try:
        # Just build the query - don't execute it
        # This validates field names and lookup types
        model.objects.filter(**match).query
    except Exception as e:
        raise ValueError(f"Invalid match filter for {event_type}: {e}") from e


def hold(
    event_type: str,
    match: dict,
    priority: int = 0,
    bump: bool = False,
    expires_in: Optional[timedelta] = DEFAULT_EXPIRY,
    expires_after_events: Optional[int] = None,
) -> Optional[ClaimInfo]:
    """
    Claim events matching the filter.

    Args:
        event_type: Event type to claim (e.g., "message_received")
        match: Django ORM filter dict applied to event's entity
        priority: Higher priority claims win when multiple match
        bump: If True, remove conflicting claims regardless of priority
        expires_in: Time-based expiry (default 60 min, None for no time limit)
        expires_after_events: Event-count expiry (None for no limit)

    Returns:
        ClaimInfo of bumped claim if one was replaced, None otherwise

    Raises:
        RuntimeError: If not called from within a handler/step execution
        ValueError: If match dict contains invalid ORM filter syntax
    """
    from .models import EventClaim

    owner = _get_current_owner()

    # Validate match dict before storing
    _validate_match(event_type, match)

    expires_at = timezone.now() + expires_in if expires_in else None

    with transaction.atomic():
        # Check for existing claim with same match
        existing = EventClaim.objects.select_for_update().filter(
            event_type=event_type,
            match=match,
        ).first()

        bumped_info = None

        if existing:
            # Check if expired
            if existing.is_expired:
                existing.delete()
                existing = None
            # Check if we already own it
            elif (existing.owner_type == owner.owner_type and
                  existing.owner_class == owner.owner_class.__name__ and
                  existing.owner_run_id == owner.owner_run_id):
                # Refresh the claim
                existing.priority = priority
                existing.expires_at = expires_at
                existing.max_events = expires_after_events
                existing.save()
                return None
            # Someone else owns it
            elif bump or priority > existing.priority:
                # Capture info before deleting
                bumped_info = ClaimInfo(
                    event_type=existing.event_type,
                    match=existing.match,
                    owner_type=existing.owner_type,
                    owner_class=existing.owner_class,
                    owner_run_id=existing.owner_run_id,
                    priority=existing.priority,
                    expires_at=existing.expires_at,
                    max_events=existing.max_events,
                    events_handled=existing.events_handled,
                    claimed_at=existing.claimed_at,
                )
                existing.delete()
                existing = None
            else:
                raise ValueError(
                    f"Cannot claim {event_type} with match {match}: held by {existing.owner_class} "
                    f"with priority {existing.priority} (yours: {priority}). "
                    f"Use bump=True to force takeover."
                )

        if existing is None:
            EventClaim.objects.create(
                event_type=event_type,
                match=match,
                owner_type=owner.owner_type,
                owner_class=owner.owner_class.__name__,
                owner_run_id=owner.owner_run_id,
                priority=priority,
                expires_at=expires_at,
                max_events=expires_after_events,
            )

        return bumped_info


def release(event_type: str, match: dict) -> bool:
    """
    Release a specific claim.

    Args:
        event_type: The event type to release
        match: The exact match dict used when claiming

    Returns:
        True if a claim was released, False if no matching claim existed

    Raises:
        RuntimeError: If not called from within a handler/step execution
        ValueError: If trying to release a claim you don't own
    """
    from .models import EventClaim

    owner = _get_current_owner()

    with transaction.atomic():
        existing = EventClaim.objects.select_for_update().filter(
            event_type=event_type,
            match=match,
        ).first()

        if not existing:
            return False

        # Verify ownership
        if not (existing.owner_type == owner.owner_type and
                existing.owner_class == owner.owner_class.__name__ and
                existing.owner_run_id == owner.owner_run_id):
            raise ValueError(
                f"Cannot release {event_type}: owned by {existing.owner_class}, not you."
            )

        existing.delete()
        return True


def release_all_for_owner(owner_type: str, owner_run_id: int) -> int:
    """
    Release all claims owned by a specific owner.

    Used for auto-cleanup when agent/workflow completes.

    Args:
        owner_type: "agent" or "workflow"
        owner_run_id: The run ID

    Returns:
        Number of claims released
    """
    from .models import EventClaim

    count, _ = EventClaim.objects.filter(
        owner_type=owner_type,
        owner_run_id=owner_run_id,
    ).delete()

    return count


def holder(event_type: str, entity: "Model") -> Optional[ClaimInfo]:
    """
    Find which claim (if any) matches this entity for this event type.

    Args:
        event_type: The event type to check
        entity: The entity to match against

    Returns:
        ClaimInfo of highest priority matching claim, or None
    """
    claim = find_matching_claim(event_type, entity)
    if not claim:
        return None

    return ClaimInfo(
        event_type=claim.event_type,
        match=claim.match,
        owner_type=claim.owner_type,
        owner_class=claim.owner_class,
        owner_run_id=claim.owner_run_id,
        priority=claim.priority,
        expires_at=claim.expires_at,
        max_events=claim.max_events,
        events_handled=claim.events_handled,
        claimed_at=claim.claimed_at,
    )


def find_matching_claim(event_type: str, entity: "Model") -> Optional["EventClaim"]:
    """
    Find the highest priority non-expired claim that matches this entity.

    Used by dispatch to determine claim ownership.

    Args:
        event_type: The event type to check
        entity: The entity to match against

    Returns:
        The matching EventClaim with highest priority, or None
    """
    from .models import EventClaim

    now = timezone.now()
    model_class = entity.__class__

    # Get all potentially matching claims for this event type
    # Filter out time-expired ones at DB level
    claims = EventClaim.objects.filter(
        event_type=event_type,
    ).filter(
        # Not time-expired (or no time expiry)
        models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now)
    ).order_by('-priority')

    for claim in claims:
        # Check event count expiry
        if claim.max_events is not None and claim.events_handled >= claim.max_events:
            continue

        # Check if entity matches the claim's filter
        try:
            if model_class.objects.filter(pk=entity.pk, **claim.match).exists():
                return claim
        except Exception:
            # Invalid match filter - skip this claim
            continue

    return None


# Import models.Q for the filter
from django.db import models
