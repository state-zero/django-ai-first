import functools
import inspect
from typing import Callable, Any, Dict


def with_context(context_priority: str = "context"):
    """
    Decorator that auto-injects context from agent's create_context() method.

    Only injects values for parameters that the function actually declares.
    The context can be anything - dict, Pydantic model, dataclass, custom object, etc.

    Args:
        context_priority: 'context' or 'llm' - which wins on parameter conflicts
                         'context' = context values override LLM tool args
                         'llm' = LLM tool args override context values
    """

    def decorator(func: Callable) -> Callable:
        # Get function signature once at decoration time
        func_signature = inspect.signature(func)
        func_param_names = set(func_signature.parameters.keys())

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            from .context import get_current_context

            current_context = get_current_context()
            if not current_context:
                return func(*args, **kwargs)

            # Get agent context if available
            agent_context_data = None
            if (
                hasattr(current_context, "_agent_instance")
                and current_context._agent_instance
            ):
                agent = current_context._agent_instance
                if hasattr(agent, "create_context") and callable(agent.create_context):
                    try:
                        agent_context_data = agent.create_context()
                    except Exception as e:
                        print(f"Warning: Error creating agent context: {e}")
                        agent_context_data = None

            if agent_context_data is None:
                return func(*args, **kwargs)

            # Extract only the values that match function parameters
            context_kwargs = _extract_matching_params(
                agent_context_data, func_param_names
            )

            # Merge only the relevant context values based on priority
            final_kwargs = kwargs.copy()

            for param_name, context_value in context_kwargs.items():
                if context_priority == "context":
                    # Context wins - always use context value if available
                    final_kwargs[param_name] = context_value
                else:
                    # LLM wins - only use context value if LLM didn't provide it
                    if param_name not in kwargs:
                        final_kwargs[param_name] = context_value

            return func(*args, **final_kwargs)

        # Preserve original function metadata for LLM introspection
        wrapper.__signature__ = func_signature
        wrapper.__doc__ = func.__doc__
        wrapper.__name__ = func.__name__
        wrapper.__annotations__ = getattr(func, "__annotations__", {})

        return wrapper

    return decorator


def _extract_matching_params(context_data: Any, param_names: set) -> Dict[str, Any]:
    """Extract only the values from context that match function parameter names."""
    if context_data is None:
        return {}

    extracted = {}

    # Try different approaches to get values
    if isinstance(context_data, dict):
        # Simple dict case
        for param_name in param_names:
            if param_name in context_data:
                extracted[param_name] = context_data[param_name]

    elif hasattr(context_data, "dict") and callable(context_data.dict):
        # Pydantic model case
        try:
            context_dict = context_data.dict()
            for param_name in param_names:
                if param_name in context_dict:
                    extracted[param_name] = context_dict[param_name]
        except:
            # Fall back to attribute access
            for param_name in param_names:
                if hasattr(context_data, param_name):
                    extracted[param_name] = getattr(context_data, param_name)

    elif hasattr(context_data, "__dict__"):
        # General object/dataclass case
        for param_name in param_names:
            if hasattr(context_data, param_name):
                extracted[param_name] = getattr(context_data, param_name)

    else:
        # Last resort - try to treat as dict-like
        try:
            for param_name in param_names:
                if param_name in context_data:
                    extracted[param_name] = context_data[param_name]
        except (TypeError, KeyError):
            pass

    return extracted
