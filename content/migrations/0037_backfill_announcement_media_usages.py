"""Bring ticker artwork into the media library.

Third companion to `0023_backfill_media_usages` (the four original `content`
fields) and `0032_backfill_course_media_usages` (the two in `courses`). This
one covers `Announcement.image`, added in `0035` for the live ticker.

**Expected to find nothing on its first run, and that is correct.** The field
is brand new, so no row can have carried an image before `0035`. It exists
because `content/media.py`'s `OWNED_IMAGE_FIELDS` is the contract the Pictures
screen and the 409 delete guard are built on, and
`test_every_owned_field_is_covered_by_some_backfill` enforces that every entry
in it is covered by a backfill. A field registered there with no backfill is
the exact drift that test was written to catch — see its docstring.

It is not purely theoretical either: a deploy applies `0035` and `0037` in the
same run, but a re-run against a database where `0035` landed earlier (a
rehearsal copy, a box migrated in two steps) can genuinely find rows. It is
idempotent via `get_or_create`, so running it either way is safe.

From here on `content/media_signals.py` keeps `MediaUsage` current on every
write — that is derived data and must stay correct on every path, which is why
a signal is right for usages and wrong for revisions.

⚠ `object_id` is written with `str()`, matching `0032`. An int and its string
form are distinct rows to the unique constraint, so mixing the two would
double-count and make "used on N pages" wrong.
"""
from django.db import migrations

ANNOUNCEMENT_IMAGE_FIELDS = [
    ("content", "Announcement", "image"),
]


def backfill(apps, schema_editor):
    ContentImage = apps.get_model("content", "ContentImage")
    MediaUsage = apps.get_model("content", "MediaUsage")
    ContentType = apps.get_model("contenttypes", "ContentType")

    for app_label, model_name, field in ANNOUNCEMENT_IMAGE_FIELDS:
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
    """Remove only this field's usages.

    Filtered on `field_name` as well as the content type, because unlike
    `courses.Course`/`Board` the `Announcement` content type could later own a
    second image field — deleting by content type alone would then take that
    one's usages with it.
    """
    MediaUsage = apps.get_model("content", "MediaUsage")
    ContentType = apps.get_model("contenttypes", "ContentType")
    for app_label, model_name, field in ANNOUNCEMENT_IMAGE_FIELDS:
        ct = ContentType.objects.filter(
            app_label=app_label, model=model_name.lower(),
        ).first()
        if ct is not None:
            MediaUsage.objects.filter(content_type=ct, field_name=field).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0036_backfill_announcement_slots"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
