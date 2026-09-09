"""Keep ``MediaUsage`` in step with whatever the owning rows point at.

design_handoff_content_studio Phase 4.

⚠ This IS a ``post_save`` signal, deliberately — and that is not a
contradiction of the rule in ``revisions.py`` that forbids one.

``ContentRevision`` is a record of *what a person did*, so a signal firing
during migrations, seed commands and the public site's own writes fills the
History screen with entries nobody caused. ``MediaUsage`` is *derived data*: a
join table that must be correct no matter which code path wrote the image —
admin API, Django admin, a management command, or a shell. For that, firing on
every write is exactly what you want.

The cost of getting this wrong is asymmetric, too: a stale usage row either
blocks a legitimate delete forever, or lets someone delete a picture that a
live page still renders.
"""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver


def _owner_models():
    from django.apps import apps as global_apps

    from .media import EMBEDDING_FIELDS, OWNED_IMAGE_FIELDS

    # Both lists: a model that only EMBEDS pictures (never owns one in a
    # FileField) still has to be watched, or its body edits never update the
    # usage table. BlogPost happens to be in both today, so leaving embedding
    # out would have worked by accident and broken on the next model added.
    out = []
    for app_label, model_name, _field in [*OWNED_IMAGE_FIELDS, *EMBEDDING_FIELDS]:
        try:
            model = global_apps.get_model(app_label, model_name)
        except LookupError:
            continue
        if model not in out:
            out.append(model)
    return out


def _on_owner_saved(sender, instance, **kwargs):
    from .media import sync_usages_for

    # Never let bookkeeping break a real save. A missed sync shows a wrong
    # usage count; a raised exception loses the editor's work.
    try:
        sync_usages_for(instance)
    except Exception:  # noqa: BLE001
        pass


def _on_owner_deleted(sender, instance, **kwargs):
    from django.contrib.contenttypes.models import ContentType

    from .models import MediaUsage

    try:
        ct = ContentType.objects.get_for_model(sender)
        # str() — object_id is a CharField now that owners span UUID-PK
        # models in `courses`. Passing a raw UUID here matches nothing, so
        # deleting a course would strand its usage row and permanently block
        # that picture from ever being deleted from the library.
        MediaUsage.objects.filter(
            content_type=ct, object_id=str(instance.pk),
        ).delete()
    except Exception:  # noqa: BLE001
        pass


def connect():
    for model in _owner_models():
        post_save.connect(
            _on_owner_saved, sender=model,
            dispatch_uid=f"media_usage_save_{model._meta.label_lower}",
        )
        post_delete.connect(
            _on_owner_deleted, sender=model,
            dispatch_uid=f"media_usage_delete_{model._meta.label_lower}",
        )
