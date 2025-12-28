"""
Centralized settings for django-ai-first.

All settings are accessed via the DJANGO_AI dict in Django settings.

Example settings.py:

    DJANGO_AI = {
        'PUSHER': {
            'APP_ID': '...',
            'KEY': '...',
            'SECRET': '...',
            'CLUSTER': 'us2',
        },
        'TIKA_SERVER_ENDPOINT': 'http://localhost:9998',
        'WORKFLOW_TESTING_MODE': False,
        'SKIP_Q2_AUTOSCHEDULE': False,
    }
"""
from django.conf import settings


def get_setting(key: str, default=None):
    """
    Get a setting from the DJANGO_AI dict.

    Args:
        key: The setting key (e.g., 'PUSHER', 'TIKA_SERVER_ENDPOINT')
        default: Default value if not set

    Returns:
        The setting value or default
    """
    django_ai_settings = getattr(settings, 'DJANGO_AI', {})
    return django_ai_settings.get(key, default)


def get_pusher_config() -> dict:
    """Get Pusher configuration dict."""
    return get_setting('PUSHER', {})


def get_tika_server_endpoint() -> str | None:
    """Get Tika server endpoint URL."""
    return get_setting('TIKA_SERVER_ENDPOINT')


def is_workflow_testing_mode() -> bool:
    """Check if workflow testing mode is enabled."""
    return get_setting('WORKFLOW_TESTING_MODE', False)


def should_skip_q2_autoschedule() -> bool:
    """Check if Q2 auto-scheduling should be skipped."""
    return get_setting('SKIP_Q2_AUTOSCHEDULE', False)
