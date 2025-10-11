from django.apps import AppConfig


class FilesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'django_ai.files'
    verbose_name = 'File Management'

    def ready(self):
        # Import signal handlers
        from . import signals
