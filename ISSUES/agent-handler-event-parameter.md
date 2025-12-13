# Agent handlers should receive the event when requested

## Problem

Agent handlers are only called with `context`, even when the handler signature requests `event`:

```python
# In django_ai/automation/agents/core.py:665
handler_method(context)  # Only passes context
```

This means handlers cannot access:
- `event.entity` - the model instance that fired the event
- `event.event_name` - which event triggered this
- Any other event metadata

## Current behavior

```python
@handler("account_associated", match={"shadow_user": "{{ ctx.guest_id }}"})
def handle_account_associated(self, ctx: Context, event: Event):
    token = event.entity  # FAILS - event is not passed
```

The handler receives a `TypeError` because `event` parameter is not provided.

## Expected behavior

If a handler's signature includes `event`, it should be passed:

```python
@handler("account_associated", match={"shadow_user": "{{ ctx.guest_id }}"})
def handle_account_associated(self, ctx: Context, event: Event):
    token = event.entity  # Works - can access the entity directly
    real_user = token.user
```

## Proposed fix

In `django_ai/automation/agents/core.py` around line 665:

```python
import inspect

# Check if handler wants event parameter
sig = inspect.signature(handler_method)
params = list(sig.parameters.keys())

if 'event' in params:
    handler_method(context, event)
else:
    handler_method(context)
```

This maintains backward compatibility - existing handlers that only take `ctx` continue to work.

## Location

`django_ai/automation/agents/core.py:665` in `_execute_handler` or similar function.

## Impact

Without this fix, handlers that need to access the event's entity must use workarounds like querying for the entity:

```python
# Workaround - have to query instead of using event.entity
token = AccountAssociationToken.objects.filter(
    shadow_user_id=ctx.guest_id,
    is_used=True,
).order_by('-created_at').first()
```

This is inefficient and error-prone compared to simply accessing `event.entity`.
