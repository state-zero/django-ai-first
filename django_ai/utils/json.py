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
    """
    try:
        return model.model_dump(mode="json")
    except Exception as e:
        logger.warning(
            f"model_dump(mode='json') failed for {model.__class__.__name__}, using DjangoJSONEncoder fallback: {e}"
        )
        raw_dict = model.model_dump()
        return json.loads(json.dumps(raw_dict, cls=DjangoJSONEncoder))
