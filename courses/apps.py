from django.apps import AppConfig


class CoursesConfig(AppConfig):
    name = "courses"

    def ready(self):
        # Models split across extra modules must be imported so Django
        # registers them. Doing it here (rather than relying on admin.py or the
        # URLconf importing them) guarantees registration regardless of import
        # order — including the new BatchChapterProgress model.
        from . import (  # noqa: F401
            models_recordings,
            models_progress,
            models_batch_progress,
        )
