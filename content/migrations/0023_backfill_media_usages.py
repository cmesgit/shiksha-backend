"""Backfill the media library from images already attached to rows.

design_handoff_content_studio Phase 4.

Before this, a picture lived only on the row that owned it. This walks every
owning image field, makes sure the library has a `ContentImage` for each
distinct stored file, and records a `MediaUsage` per reference — so "used on 2
pages" is true for content that predates the library.

⚠ The owning field on a post is `BlogPost.cover`, not `cover_image`. Naming it
wrong makes this migration silently find nothing and report success.

Reverse deletes only the usage rows. `ContentImage` rows are deliberately kept:
some existed before this migration (the rich-text editor has uploaded into that
table for a long time) and there is no safe way to tell those apart from ones
this created without recording extra state nobody would ever read.
"""
from django.db import migrations

OWNED_IMAGE_FIELDS = [
    ("content", "BlogPost", "cover"),
    ("content", "ShowcaseCourse", "image"),
    ("content", "HomeContentBlock", "image"),
    ("content", "HomeListItem", "image"),
]


def backfill(apps, schema_editor):
    ContentImage = apps.get_model("content", "ContentImage")
    MediaUsage = apps.get_model("content", "MediaUsage")
    ContentType = apps.get_model("contenttypes", "ContentType")

    for app_label, model_name, field in OWNED_IMAGE_FIELDS:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            continue

        ct, _ = ContentType.objects.get_or_create(
            app_label=app_label, model=model_name.lower(),
        )

        rows = model.objects.exclude(**{field: ""}).exclude(**{f"{field}__isnull": True})
        for obj in rows.iterator():
            name = getattr(getattr(obj, field, None), "name", "") or ""
            if not name:
                continue
            asset = ContentImage.objects.filter(file=name).first()
            if asset is None:
                asset = ContentImage.objects.create(
                    file=name,
                    original_name=name.rsplit("/", 1)[-1][:200],
                )
            MediaUsage.objects.get_or_create(
                asset=asset, content_type=ct, object_id=obj.pk, field_name=field,
            )


def unbackfill(apps, schema_editor):
    apps.get_model("content", "MediaUsage").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0022_contentimage_original_name_mediausage"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
