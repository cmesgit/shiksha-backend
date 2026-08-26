"""Media library: which picture is used where.

design_handoff_content_studio Phase 4.

⚠ There is no ``MediaAsset``. The handoff spec proposed one, but
``ContentImage`` already had six of its seven fields, auto-populates its own
dimensions, and is already routed at ``admin/editor-images``. Building a second
image table would have left the CMS with two libraries and two upload
endpoints. Only ``original_name`` was missing, plus ``MediaUsage`` to record
the reverse direction.

⚠ The owning field on a post is ``BlogPost.cover``, NOT ``cover_image`` as the
spec says. Getting that wrong makes the backfill silently find nothing.
"""
from django.contrib.contenttypes.models import ContentType

from .models import ContentImage, MediaUsage

# Every (model, field) that can point at a picture. Adding an image field
# anywhere means adding it here, or the Pictures screen will under-report and
# a delete that breaks a live page will be allowed through.
OWNED_IMAGE_FIELDS = [
    ("content", "BlogPost", "cover"),
    ("content", "ShowcaseCourse", "image"),
    ("content", "HomeContentBlock", "image"),
    ("content", "HomeListItem", "image"),
]


def _iter_owners(apps=None):
    """Yield (model, field_name) for every owning image field."""
    getter = apps.get_model if apps else _default_get_model
    for app_label, model_name, field in OWNED_IMAGE_FIELDS:
        try:
            yield getter(app_label, model_name), field
        except LookupError:
            continue


def _default_get_model(app_label, model_name):
    from django.apps import apps as global_apps
    return global_apps.get_model(app_label, model_name)


def asset_for_file(name, apps=None, defaults=None):
    """Find (or create) the library row for a stored file path."""
    model = apps.get_model("content", "ContentImage") if apps else ContentImage
    asset = model.objects.filter(file=name).first()
    if asset is not None:
        return asset, False
    payload = {"file": name}
    payload.update(defaults or {})
    if "original_name" not in payload:
        payload["original_name"] = name.rsplit("/", 1)[-1]
    return model.objects.create(**payload), True


def sync_usages_for(obj, field_names=None):
    """Make ``MediaUsage`` agree with what ``obj``'s image fields point at now.

    Called after an owner is saved. Removes stale rows as well as adding new
    ones — without the removal half, swapping an image out of a post would
    leave the old one reporting a usage that no longer exists, and it could
    never be deleted.
    """
    fields = field_names or [
        f for (_, model_name, f) in OWNED_IMAGE_FIELDS
        if model_name == obj.__class__.__name__
    ]
    if not fields:
        return

    ct = ContentType.objects.get_for_model(obj.__class__)
    for field in fields:
        stored = getattr(obj, field, None)
        name = getattr(stored, "name", None) or ""
        existing = MediaUsage.objects.filter(
            content_type=ct, object_id=obj.pk, field_name=field,
        )
        if not name:
            existing.delete()
            continue
        asset, _ = asset_for_file(name)
        existing.exclude(asset=asset).delete()
        MediaUsage.objects.get_or_create(
            asset=asset, content_type=ct, object_id=obj.pk, field_name=field,
        )


def usage_payload(asset):
    """``used_in[]`` — what a delete would break, in words a person can act on."""
    out = []
    for usage in asset.usages.select_related("content_type"):
        target = usage.target
        label = usage.content_type.name
        title = ""
        if target is not None:
            for candidate in ("title", "heading", "question", "message", "label"):
                value = getattr(target, candidate, "")
                if value:
                    title = str(value)
                    break
        out.append({
            "kind": usage.content_type.model,
            "kind_label": label,
            "title": title or f"{label} #{usage.object_id}",
            "field": usage.field_name,
            "object_id": usage.object_id,
        })
    return out
