# Exports are available after Django apps are loaded
# Use: from django_ai.automation.agents import agent, handler, ...

__all__ = [
    # Decorators
    "agent",
    "handler",
    # Engine
    "agent_engine",
    # Manager
    "AgentManager",
    # Types
    "HandlerInfo",
    # Models
    "AgentRun",
    "AgentStatus",
    "HandlerExecution",
    "HandlerStatus",
]


def __getattr__(name):
    """Lazy import to avoid loading models before Django is ready."""
    if name in ("agent", "handler", "agent_engine", "AgentManager", "HandlerInfo"):
        from .core import agent, handler, agent_engine, AgentManager, HandlerInfo
        return locals()[name]
    elif name in ("AgentRun", "AgentStatus", "HandlerExecution", "HandlerStatus"):
        from .models import AgentRun, AgentStatus, HandlerExecution, HandlerStatus
        return locals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
