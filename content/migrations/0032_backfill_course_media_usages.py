"""Bring course and board artwork into the media library.

Companion to `0023_backfill_media_usages`, which did the same for the four
`content`-app image fields. This one covers the two in `courses`:
`Course.thumbnail` and `Board.logo`.

Why they were missing, and why it matters: `Course.thumbnail` is the picture
BOTH public surfaces read. `/courses` reads it directly, and the homepage's
featured grid prefers it ahead of the showcase card's own image
(`PublicFeaturedView`'s fallback chain). So it is the most load-bearing image
on the site, and the library had never heard of it — it reported no usage
count, and the 409 delete guard could not stop someone removing it out from
under a live course.

⚠ Depends on `0031_alter_mediausage_object_id`. Course and Board have **UUID**
primary keys, which do not fit the `PositiveIntegerField` this table shipped
with. Running this against the narrow column raises rather than silently
skipping, but the ordering makes that unreachable.

⚠ `object_id` is written with `str()`. An int and its string form are distinct
rows to the unique constraint, so mixing the two would double-count usages and
make the delete guard's "used on N pages" wrong.

Reverse removes only the usages this added — the two `courses` content types —
rather than emptying the table as `0023`'s reverse does, so rolling this back
does not discard the content-app usages that migration created.
"""
from django.db import migrations

COURSE_IMAGE_FIELDS = [
    ("courses", "Course", "thumbnail"),
    ("courses", "Board", "logo"),
]


def backfill(apps, schema_editor):
    ContentImage = apps.get_model("content", "ContentImage")
    MediaUsage = apps.get_model("content", "MediaUsage")
    ContentType = apps.get_model("contenttypes", "ContentType")

    for app_label, model_name, field in COURSE_IMAGE_FIELDS:
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
                asset=asset, content_type=ct, object_id=str(obj.pk),
                field_name=field,
            )


def unbackfill(apps, schema_editor):
    MediaUsage = apps.get_model("content", "MediaUsage")
    ContentType = apps.get_model("contenttypes", "ContentType")
    for app_label, model_name, _field in COURSE_IMAGE_FIELDS:
        ct = ContentType.objects.filter(
            app_label=app_label, model=model_name.lower(),
        ).first()
        if ct is not None:
            MediaUsage.objects.filter(content_type=ct).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0031_alter_mediausage_object_id"),
        ("courses", "0039_sessionrecording_trim_end_seconds_and_more"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
