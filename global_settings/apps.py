from django.apps import AppConfig


class GlobalSettingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "global_settings"
    verbose_name = "Global settings"
