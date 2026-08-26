from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _sync_content_editors_group(sender, **kwargs):
    # Runs setup_content_editors after every `migrate` so the "Content
    # Editors" group always exists with the right permissions, instead of
    # relying on someone remembering to run it manually. get_or_create +
    # .set() inside the command make this idempotent.
    from django.core.management import call_command

    call_command("setup_content_editors", verbosity=0)


class ContentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "content"
    verbose_name = "Content (CMS)"

    def ready(self):
        # Registers post_save/post_delete signals that bump the content
        # cache version, so list endpoints invalidate instantly on edit.
        from . import cache  # noqa: F401

        # Keeps MediaUsage correct on every write path (Phase 4). Unlike
        # ContentRevision, this is derived data — see media_signals.py for why
        # a signal is right here and wrong there.
        from . import media_signals

        media_signals.connect()

        post_migrate.connect(_sync_content_editors_group, sender=self)
