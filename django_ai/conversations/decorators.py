"""
Context injection decorators.

Note: with_context is deprecated. Use self.context directly instead.
"""

import functools
from typing import Callable


def with_context(context_priority: str = "context"):
    """
    DEPRECATED: Context is now accessed via self.context directly.

    This decorator is kept for backwards compatibility but does nothing.
    Update your code to use self.context instead.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper
    return decorator
