# Feature: Use jsonpickle for robust workflow context serialization

## Summary

When a workflow step uses `PrimaryKeyRelatedField(many=True)` or stores Django model instances in the Pydantic context, serialization fails because neither Pydantic's `model_dump(mode="json")` nor `DjangoJSONEncoder` can handle Django model instances.

Rather than handling edge cases one by one, we should use `jsonpickle` for context serialization. This provides:
- **Automatic handling** of Django models, datetimes, and arbitrary Python objects
- **Human-readable JSON** output (unlike base64 pickle)
- **Debuggable** workflow state in the database
- **Round-trip serialization** - objects are fully reconstructed, not just converted to PKs

## Current Error

```
pydantic_core._pydantic_core.PydanticSerializationError: Unable to serialize unknown type: <class 'business.guidebooks.models.GetInStep'>

TypeError: Object of type GetInStep is not JSON serializable
```

## Why jsonpickle?

We considered several approaches:

| Approach | Pros | Cons |
|----------|------|------|
| Handle edge cases in JSON | Standard JSON output | Endless edge cases, models become PKs only |
| pickle + base64 | Handles everything | Opaque blob, not debuggable |
| dill/cloudpickle | Handles lambdas, closures | Overkill - we don't need lambda serialization |
| **jsonpickle** | Handles everything, readable JSON | Small dependency |

jsonpickle output is human-readable:
```json
{
    "py/object": "business.guidebooks.models.GetInStep",
    "id": 1,
    "title": "Enter building code",
    "order": 1
}
```

## Proposed Fix

Replace `safe_model_dump()` with jsonpickle-based serialization:

```python
import jsonpickle
import logging
from typing import Dict, Any
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Configure jsonpickle for readable output
jsonpickle.set_preferred_backend('json')
jsonpickle.set_encoder_options('json', ensure_ascii=False, indent=None)


def safe_model_dump(model: BaseModel) -> Dict[str, Any]:
    """
    Serialize a Pydantic model to a JSON-serializable dict using jsonpickle.

    This handles Django models, datetimes, and arbitrary Python objects
    while producing human-readable JSON output.
    """
    raw_dict = model.model_dump()
    # jsonpickle.encode returns a JSON string, decode it back to dict
    return jsonpickle.decode(jsonpickle.encode(raw_dict, unpicklable=True))


def safe_model_load(data: Dict[str, Any], model_class: type[BaseModel]) -> BaseModel:
    """
    Deserialize a dict back to a Pydantic model, reconstructing any
    serialized Python objects (Django models, etc).
    """
    # Decode any jsonpickle-encoded objects in the data
    restored_data = jsonpickle.decode(jsonpickle.encode(data))
    return model_class.model_validate(restored_data)
```

## Test Cases

```python
from django.test import TestCase
from django.db import models
from pydantic import BaseModel
from typing import List, Any
from datetime import datetime
from decimal import Decimal
from django_ai.utils.json import safe_model_dump, safe_model_load


class GetInStep(models.Model):
    title = models.CharField(max_length=100)
    order = models.IntegerField(default=1)

    class Meta:
        app_label = 'guidebooks'


class TestContext(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    items: List[Any] = []
    created_at: datetime = None
    amount: Decimal = None


class JsonPickleSerializationTests(TestCase):
    def test_serialize_django_model_instances(self):
        """Django model instances are serialized and can be reconstructed."""
        instance = GetInStep(pk=1, title="Test Step", order=1)
        ctx = TestContext(items=[instance])

        # Serialize
        data = safe_model_dump(ctx)

        # Should be JSON-serializable dict
        self.assertIsInstance(data, dict)

        # Should contain type info for reconstruction
        self.assertIn('py/object', str(data))

    def test_round_trip_django_models(self):
        """Django models survive serialization round-trip."""
        instance = GetInStep(pk=1, title="Test Step", order=1)
        ctx = TestContext(items=[instance])

        # Round trip
        data = safe_model_dump(ctx)
        restored = safe_model_load(data, TestContext)

        # Model should be reconstructed
        self.assertEqual(restored.items[0].pk, 1)
        self.assertEqual(restored.items[0].title, "Test Step")

    def test_serialize_datetime_and_decimal(self):
        """Standard Python types are handled."""
        ctx = TestContext(
            items=[],
            created_at=datetime(2024, 1, 15, 10, 30),
            amount=Decimal("99.99")
        )

        data = safe_model_dump(ctx)
        restored = safe_model_load(data, TestContext)

        self.assertEqual(restored.created_at, ctx.created_at)
        self.assertEqual(restored.amount, ctx.amount)

    def test_human_readable_output(self):
        """Output should be inspectable JSON, not opaque blob."""
        instance = GetInStep(pk=1, title="Test Step", order=1)
        ctx = TestContext(items=[instance])

        data = safe_model_dump(ctx)

        # Should be able to see the model data
        import json
        json_str = json.dumps(data)
        self.assertIn("Test Step", json_str)
```

## Dependencies

Add to `requirements.txt`:
```
jsonpickle>=3.0.0
```

## Files to Modify

- `django_ai/utils/json.py` - Replace implementation
- `django_ai/automation/workflows/core.py` - Update to use `safe_model_load` when reconstructing context
- `requirements.txt` / `pyproject.toml` - Add jsonpickle dependency

## Priority

Medium - This affects any workflow that uses M2M or FK fields via `PrimaryKeyRelatedField`, or stores any non-JSON-serializable types in context.
