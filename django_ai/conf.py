"""
Django AI settings configuration.

All settings are namespaced under a single DJANGO_AI dict in Django settings:

    DJANGO_AI = {
        'TIMERS_IN_PROCESS': True,  # True for local dev, False for production
        'PUSHER': {
            'APP_ID': '...',
            'KEY': '...',
            'SECRET': '...',
            'CLUSTER': '...',
        },
        'TEXT_EXTRACTOR': 'myapp.extractors.CustomExtractor',
    }

Settings:
    TIMERS_IN_PROCESS: bool (default: None)
        - True: Execute timed tasks in-process via background thread (for local dev)
        - False: Require 'python manage.py run_timers' worker (for production)
        - None: Will warn on first use that tasks won't run without worker

    PUSHER: dict (optional)
        Pusher configuration for real-time streaming.

    TEXT_EXTRACTOR: str (optional)
        Dotted path to custom text extractor class.
"""

import warnings
from typing import Any, Optional
from django.conf import settings

# Track if we've already warned about TIMERS_IN_PROCESS
_timers_warned = False


def get_settings() -> dict:
    """Get the DJANGO_AI settings dict, with fallback to legacy settings."""
    ai_settings = getattr(settings, 'DJANGO_AI', {})

    # Backwards compatibility: merge legacy DJANGO_AI_PUSHER if present
    if not ai_settings.get('PUSHER'):
        legacy_pusher = getattr(settings, 'DJANGO_AI_PUSHER', None)
        if legacy_pusher:
            ai_settings = {**ai_settings, 'PUSHER': legacy_pusher}

    return ai_settings


def get(key: str, default: Any = None) -> Any:
    """Get a specific setting from DJANGO_AI."""
    return get_settings().get(key, default)


def get_timers_in_process() -> Optional[bool]:
    """
    Get the TIMERS_IN_PROCESS setting.

    Returns:
        True: In-process mode - background thread executes tasks
        False: Worker mode - require 'python manage.py run_timers'
        None: Not explicitly set
    """
    return get_settings().get('TIMERS_IN_PROCESS')


def is_timers_in_process_mode() -> bool:
    """
    Check if timers should run in-process via background thread.

    Returns True if TIMERS_IN_PROCESS is explicitly True.
    Returns False otherwise (including when not set).

    If not explicitly set, issues a one-time warning.
    """
    global _timers_warned

    setting = get_timers_in_process()

    if setting is True:
        return True

    if setting is None and not _timers_warned:
        _timers_warned = True
        warnings.warn(
            "DJANGO_AI['TIMERS_IN_PROCESS'] is not set. Timed tasks (schedule_task, "
            "debounce_task) will not execute without running 'python manage.py "
            "run_timers'. Set TIMERS_IN_PROCESS=True for local dev or "
            "TIMERS_IN_PROCESS=False to silence this warning.",
            UserWarning,
            stacklevel=3,
        )

    return False


def get_pusher_config() -> dict:
    """Get Pusher configuration."""
    return get('PUSHER', {})


def reset_warnings():
    """Reset warning state (for testing)."""
    global _timers_warned
    _timers_warned = False
