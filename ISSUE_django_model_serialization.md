# Bug: jsonpickle py/id references break due to JSON round-trip

## Summary

Django model instances with circular references fail to deserialize from workflow context, returning `[]` instead of the model instance. This causes Pydantic validation errors and 500 errors in production.

## Root Cause

The bug is in `django_ai/utils/json.py`. The current implementation converts jsonpickle output to a dict, which breaks `py/id` circular references when JSON key order changes.

**Current broken implementation:**

```python
def safe_model_dump(model: BaseModel) -> Dict[str, Any]:
    # BUG: json.loads() converts to dict, losing guaranteed key order
    return json.loads(jsonpickle.encode(model.model_dump()))

def restore_model_data(data: Dict[str, Any]) -> Dict[str, Any]:
    # BUG: json.dumps() re-serializes with potentially different key order
    return jsonpickle.decode(json.dumps(data))
```

**Why this breaks:**

1. `jsonpickle.encode()` produces a JSON string with `py/id` references for circular objects
2. `json.loads()` converts this to a Python dict
3. Dict is stored in PostgreSQL JSONField
4. PostgreSQL may return keys in different order
5. `json.dumps()` serializes with new key order
6. `jsonpickle.decode()` traverses in different order than encoding
7. `py/id: 2` now points to wrong object → returns `[]`

**Why tests pass:** SQLite (used in tests) preserves key order. PostgreSQL (production) doesn't guarantee it.

## Fix

Store the jsonpickle output as a **string**, not a parsed dict. This preserves the exact byte sequence including key order.

**Option A: Store as wrapped string in JSONField**

```python
def safe_model_dump(model: BaseModel) -> Dict[str, Any]:
    """Serialize a Pydantic model using jsonpickle, preserving circular references."""
    encoded = jsonpickle.encode(model.model_dump())
    return {"__jsonpickle__": encoded}

def restore_model_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Restore a Pydantic model from jsonpickle-encoded data."""
    if "__jsonpickle__" in data:
        return jsonpickle.decode(data["__jsonpickle__"])
    # Backwards compatibility: try old format
    return jsonpickle.decode(json.dumps(data))
```

**Option B: Change WorkflowRun.data to TextField**

```python
# In models.py
class WorkflowRun(models.Model):
    data = models.TextField(default="{}")  # Was JSONField

# In json.py
def safe_model_dump(model: BaseModel) -> str:
    return jsonpickle.encode(model.model_dump())

def restore_model_data(data: str) -> Dict[str, Any]:
    return jsonpickle.decode(data)
```

## Recommendation

**Option A** is recommended because:
- No database migration required
- Backwards compatible with existing data (falls back to old format)
- JSONField still provides some validation

## Test Case

```python
def test_jsonpickle_roundtrip_preserves_circular_refs():
    """Verify circular references survive storage and retrieval."""
    # Create models with circular reference
    home = HomeFactory()
    policy = CheckinPolicy.objects.create(home=home, tenant=home.tenant)
    _ = policy.home  # Cache home in policy
    _ = home.checkin_policy  # Cache policy in home (circular!)

    class Context(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        initiated_by_user_pk: UUID
        checkin_policy: Optional[CheckinPolicy] = None

    ctx = Context(
        initiated_by_user_pk=uuid4(),
        checkin_policy=policy
    )

    # Serialize and restore
    serialized = safe_model_dump(ctx)
    restored = restore_model_data(serialized)

    # This fails with current implementation, passes with fix
    assert restored['checkin_policy'] != []
    assert isinstance(restored['checkin_policy'], CheckinPolicy)
```

## Files to Modify

- `django_ai/utils/json.py` - Fix `safe_model_dump()` and `restore_model_data()`
- `django_ai/automation/workflows/models.py` - Only if using Option B

## Impact

- **Severity**: High (500 errors in production)
- **Affected**: Any workflow storing Django models with cached reverse relations
