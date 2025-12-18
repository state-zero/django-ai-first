import jsonpickle
from typing import Dict, Any
from pydantic import BaseModel

# TODO: Consider changing WorkflowRun.data from JSONField to TextField.
# JSONField is legacy from when we used JSON serialization for persistence.
# Now we store jsonpickle output as an opaque string, so TextField would be cleaner.


def safe_model_dump(model: BaseModel) -> Dict[str, Any]:
    """Serialize a Pydantic model to a JSON-compatible dict using jsonpickle.

    Stores the jsonpickle output as a string to preserve py/id reference ordering,
    which breaks if the JSON is parsed and re-serialized with different key order.
    """
    return {"__jsonpickle__": jsonpickle.encode(model.model_dump())}


def restore_model_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Restore a dict serialized with safe_model_dump() back to Python objects."""
    return jsonpickle.decode(data["__jsonpickle__"])
