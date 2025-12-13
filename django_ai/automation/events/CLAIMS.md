# Event Claims System

The claims system allows flows (agents and workflows) to "take charge" of specific event types. When a flow claims an event type with a match condition, only that flow's handlers receive matching events - unless handlers have `ignores_claims=True` (god mode).

## Use Cases

1. **Support Ticket Flow**: Guest raises support ticket → SupportFlow claims `message_received` for `{"guest_id": 123}` → All subsequent messages from that guest route only to SupportFlow
2. **Emergency Override**: Emergency flows can use `ignores_claims=True` to always receive events regardless of claims
3. **Claim Inspection**: Flows can check who holds a claim before deciding to bump
4. **Auto-expiry**: Claims automatically expire after a configurable time or event count

## Quick Start

```python
from django_ai.automation.events import claims

@handler("ticket_created")
def on_ticket_created(self, ctx):
    # Claim all message_received events for this guest
    claims.hold("message_received", match={"guest_id": ctx.guest_id})

@handler("ticket_resolved")
def on_ticket_resolved(self, ctx):
    # Release the claim when done
    claims.release("message_received", match={"guest_id": ctx.guest_id})

# Handler that always runs regardless of claims (god mode)
@handler("message_received", ignores_claims=True)
def audit_all_messages(self, ctx):
    log_message(ctx.message)
```

## API Reference

### `claims.hold()`

Claim events matching a filter. Must be called from within a handler or workflow step.

```python
claims.hold(
    event_type: str,           # Event type to claim (e.g., "message_received")
    match: dict,               # Django ORM filter dict applied to event's entity
    priority: int = 0,         # Higher priority claims win when multiple match
    bump: bool = False,        # If True, force takeover regardless of priority
    expires_in: timedelta = timedelta(minutes=60),  # Time-based expiry (None for no limit)
    expires_after_events: int = None,  # Event-count expiry (None for no limit)
) -> Optional[ClaimInfo]
```

**Returns**: `ClaimInfo` of the bumped claim if one was replaced, `None` otherwise.

**Raises**:
- `RuntimeError`: If not called from within a handler/step execution
- `ValueError`: If match dict contains invalid ORM filter syntax
- `ValueError`: If trying to claim something already held by another flow with higher priority (and `bump=False`)

#### Examples

```python
# Basic claim with default 60-minute expiry
claims.hold("message_received", match={"guest_id": 123})

# Claim with custom time expiry
claims.hold("message_received", match={"guest_id": 123},
            expires_in=timedelta(hours=2))

# Claim with event count expiry (release after handling 5 events)
claims.hold("message_received", match={"guest_id": 123},
            expires_after_events=5)

# Claim with both expiry types (whichever comes first)
claims.hold("message_received", match={"guest_id": 123},
            expires_in=timedelta(hours=1), expires_after_events=10)

# Claim with no time expiry (only event count)
claims.hold("message_received", match={"guest_id": 123},
            expires_in=None, expires_after_events=5)

# Force takeover regardless of priority
claims.hold("message_received", match={"guest_id": 123}, bump=True)

# Higher priority claim takes over
claims.hold("message_received", match={"guest_id": 123}, priority=20)
```

### `claims.release()`

Release a specific claim. Must be called from within a handler or workflow step.

```python
claims.release(
    event_type: str,   # Event type to release
    match: dict,       # Exact match dict used when claiming
) -> bool
```

**Returns**: `True` if a claim was released, `False` if no matching claim existed.

**Raises**:
- `RuntimeError`: If not called from within a handler/step execution
- `ValueError`: If trying to release a claim you don't own

### `claims.holder()`

Find which claim (if any) matches an entity for an event type.

```python
claims.holder(
    event_type: str,   # Event type to check
    entity: Model,     # Entity to match against
) -> Optional[ClaimInfo]
```

**Returns**: `ClaimInfo` of highest priority matching claim, or `None`.

```python
# Check who holds the claim before deciding to bump
holder = claims.holder("message_received", entity=booking)
if holder:
    print(f"Claimed by {holder.owner_class} (run {holder.owner_run_id})")
    print(f"Match: {holder.match}, priority: {holder.priority}")
    if holder.owner_class == "LowPriorityFlow":
        # Safe to bump
        claims.hold("message_received", match={"guest_id": 123}, bump=True)
```

### `claims.release_all_for_owner()`

Release all claims owned by a specific owner. Used internally for auto-cleanup when agent/workflow completes.

```python
claims.release_all_for_owner(
    owner_type: str,   # "agent" or "workflow"
    owner_run_id: int, # The run ID
) -> int
```

**Returns**: Number of claims released.

## Match Syntax

Claims use Django ORM filter syntax for matching. The match dict is applied to the event's entity to determine if the claim applies.

```python
# Exact match
claims.hold("booking_confirmed", match={"guest_id": 123})

# String lookups
claims.hold("booking_confirmed", match={"guest_name__startswith": "VIP"})
claims.hold("booking_confirmed", match={"email__icontains": "@company.com"})

# Comparisons
claims.hold("high_value_order", match={"total__gte": 1000})

# Multiple conditions (AND)
claims.hold("booking_confirmed", match={
    "guest_id": 123,
    "status": "confirmed"
})

# Related fields
claims.hold("message_received", match={"booking__property_id": 456})
```

**Validation**: Match filters are validated against the model when the claim is created. Invalid field names or lookup types raise `ValueError`.

## God Mode (ignores_claims)

Handlers and workflows can bypass claim checking entirely:

```python
# Agent handler that always runs
@handler("message_received", ignores_claims=True)
def audit_all_messages(self, ctx):
    # Runs even when another flow has claimed this event
    pass

# Workflow that always starts
@event_workflow("message_received", ignores_claims=True)
class EmergencyAlertWorkflow:
    # Starts even when another flow has claimed this event
    pass
```

## Priority and Bumping

When multiple claims could match an entity, priority determines the winner:

```python
# Flow A claims with priority 10
claims.hold("message_received", match={"guest_id": 123}, priority=10)

# Flow B tries with lower priority - raises ValueError
claims.hold("message_received", match={"guest_id": 123}, priority=5)
# ValueError: Cannot claim message_received with match {...}: held by FlowA
#             with priority 10 (yours: 5). Use bump=True to force takeover.

# Flow B with higher priority can take over
bumped = claims.hold("message_received", match={"guest_id": 123}, priority=20)
# bumped contains info about the displaced claim (FlowA)

# Or force takeover regardless of priority
bumped = claims.hold("message_received", match={"guest_id": 123}, bump=True)
```

## Expiry

Claims can expire by time, event count, or both:

### Time-based Expiry

```python
# Default: 60 minutes
claims.hold("message_received", match={"guest_id": 123})

# Custom duration
claims.hold("message_received", match={"guest_id": 123},
            expires_in=timedelta(hours=4))

# No time expiry
claims.hold("message_received", match={"guest_id": 123},
            expires_in=None)
```

### Event-count Expiry

```python
# Release after handling 5 matching events
claims.hold("message_received", match={"guest_id": 123},
            expires_after_events=5)
```

### Combined Expiry

```python
# Release after 1 hour OR 10 events, whichever comes first
claims.hold("message_received", match={"guest_id": 123},
            expires_in=timedelta(hours=1), expires_after_events=10)
```

## Auto-Release

Claims are automatically released when:
- Agent completes (via `AgentEngine.complete_agent()`)
- Workflow completes successfully
- Workflow fails
- Workflow is cancelled

This prevents orphaned claims from blocking events indefinitely.

## ClaimInfo

Information returned by `holder()` about a claim:

```python
@dataclass
class ClaimInfo:
    event_type: str          # "message_received"
    match: dict              # {"guest_id": 123}
    owner_type: str          # "agent" or "workflow"
    owner_class: str         # "SupportFlow"
    owner_run_id: int        # 456
    priority: int            # 0
    expires_at: datetime     # When time-based expiry triggers
    max_events: int          # Event count limit (None if no limit)
    events_handled: int      # Events handled so far
    claimed_at: datetime     # When claim was created

    @property
    def is_expired(self) -> bool:
        # True if expired by time or event count
```

## How It Works

1. **Claiming**: When a handler calls `claims.hold()`, an `EventClaim` record is created in the database with the owner info (from execution context), match filter, and expiry settings.

2. **Dispatch**: When an event fires, `schedule_handler()` checks for matching claims:
   - Calls `find_matching_claim(event_type, entity)`
   - If a claim matches and the handler doesn't have `ignores_claims=True`:
     - If the handler's agent run IS the claim holder → schedule normally
     - If the handler's agent run is NOT the claim holder → skip scheduling

3. **Matching**: `find_matching_claim()` evaluates each claim's match filter against the entity using Django ORM:
   ```python
   if Entity.objects.filter(pk=entity.pk, **claim.match).exists():
       return claim  # This claim matches
   ```

4. **Auto-release**: When an agent/workflow completes or fails, `release_all_for_owner()` is called to clean up all claims.

## Database Model

```python
class EventClaim(models.Model):
    event_type = models.CharField(max_length=100)
    match = models.JSONField()  # Django ORM filter syntax

    owner_type = models.CharField(max_length=50)   # "agent" or "workflow"
    owner_class = models.CharField(max_length=255)
    owner_run_id = models.BigIntegerField()

    priority = models.IntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    max_events = models.IntegerField(null=True, blank=True)
    events_handled = models.IntegerField(default=0)
    claimed_at = models.DateTimeField(auto_now_add=True)
```

## Best Practices

1. **Always release claims explicitly** when done, don't rely solely on auto-expiry
2. **Use specific match filters** to avoid claiming too broadly
3. **Set reasonable expiry times** based on expected flow duration
4. **Use `ignores_claims=True` sparingly** - only for truly critical handlers like audit logging
5. **Check `holder()` before bumping** to make informed decisions
6. **Validate match syntax** by testing against your models before deploying
