"""
Context injection decorators using the clean context system.
"""

import functools
import inspect
from typing import Callable, Any, Dict
from .context import get_context


def with_context(context_priority: str = "context"):
    """
    Decorator that auto-injects context from agent's context.
    Now works with the clean context system.
    """

    def decorator(func: Callable) -> Callable:
        func_signature = inspect.signature(func)
        func_param_names = set(func_signature.parameters.keys())

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            agent_context = get_context()
            if not agent_context:
                return func(*args, **kwargs)

            # Extract matching parameters
            context_kwargs = _extract_matching_params(agent_context, func_param_names)

            # Merge based on priority
            final_kwargs = kwargs.copy()
            for param_name, context_value in context_kwargs.items():
                if context_priority == "context":
                    final_kwargs[param_name] = context_value
                else:
                    if param_name not in kwargs:
                        final_kwargs[param_name] = context_value

            return func(*args, **final_kwargs)

        wrapper.__signature__ = func_signature
        wrapper.__doc__ = func.__doc__
        wrapper.__name__ = func.__name__
        wrapper.__annotations__ = getattr(func, "__annotations__", {})
        return wrapper

    return decorator


def _extract_matching_params(context_data: Any, param_names: set) -> Dict[str, Any]:
    """Extract matching parameters from context data"""
    if context_data is None:
        return {}

    extracted = {}

    if isinstance(context_data, dict):
        for param_name in param_names:
            if param_name in context_data:
                extracted[param_name] = context_data[param_name]
    elif hasattr(context_data, "dict") and callable(context_data.dict):
        try:
            context_dict = context_data.dict()
            for param_name in param_names:
                if param_name in context_dict:
                    extracted[param_name] = context_dict[param_name]
        except:
            for param_name in param_names:
                if hasattr(context_data, param_name):
                    extracted[param_name] = getattr(context_data, param_name)
    elif hasattr(context_data, "__dict__"):
        for param_name in param_names:
            if hasattr(context_data, param_name):
                extracted[param_name] = getattr(context_data, param_name)

    return extracted
