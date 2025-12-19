import json
import logging
from typing import Dict, Any
from pydantic import BaseModel
from django.core.serializers.json import DjangoJSONEncoder

logger = logging.getLogger(__name__)


def safe_model_dump(model: BaseModel) -> Dict[str, Any]:
    """
    Safely dump a Pydantic model to a JSON-serializable dict.

    Uses Pydantic's built-in JSON serialization which handles datetime, Decimal, UUID, etc.
    Falls back to DjangoJSONEncoder for Django-specific types if needed.

    Args:
        model: Pydantic model instance to serialize

    Returns:
        Dict with JSON-serializable values

    Example:
        >>> from pydantic import BaseModel
        >>> from datetime import datetime
        >>>
        >>> class MyModel(BaseModel):
        ...     name: str
        ...     created_at: datetime
        ...
        >>> model = MyModel(name="test", created_at=datetime.now())
        >>> data = safe_model_dump(model)
        >>> # created_at is now an ISO string, not datetime object
    """
    try:
        # PRIMARY: Use model_dump with mode='json' to get JSON-serializable values
        # This handles datetime, Decimal, UUID, etc. automatically
        return model.model_dump(mode="json")
    except Exception as e:
        # FALLBACK: Use Django's JSONEncoder for Django-specific types
        logger.warning(
            f"model_dump(mode='json') failed for {model.__class__.__name__}, using DjangoJSONEncoder fallback: {e}"
        )
        raw_dict = model.model_dump()
        return json.loads(json.dumps(raw_dict, cls=DjangoJSONEncoder))
