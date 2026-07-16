from django.apps import AppConfig


class ContentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "content"
    verbose_name = "Content (CMS)"

    def ready(self):
        # Registers post_save/post_delete signals that bump the content
        # cache version, so list endpoints invalidate instantly on edit.
        from . import cache  # noqa: F401
