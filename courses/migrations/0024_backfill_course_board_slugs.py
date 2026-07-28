from django.db import migrations
from django.utils.text import slugify


def backfill_course_slugs(apps, schema_editor):
    Course = apps.get_model("courses", "Course")
    for course in Course.objects.filter(slug="").order_by("pk"):
        base = slugify(course.title)[:180] or "course"
        slug, n = base, 2
        while Course.objects.filter(slug=slug).exclude(pk=course.pk).exists():
            slug = f"{base}-{n}"
            n += 1
        course.slug = slug
        course.save(update_fields=["slug"])


def backfill_board_slugs(apps, schema_editor):
    Board = apps.get_model("courses", "Board")
    for board in Board.objects.filter(slug="").order_by("pk"):
        base = slugify(board.name)[:130] or "board"
        slug, n = base, 2
        while Board.objects.filter(slug=slug).exclude(pk=board.pk).exists():
            slug = f"{base}-{n}"
            n += 1
        board.slug = slug
        board.save(update_fields=["slug"])


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0023_coursecategory_board_display_order_board_logo_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_course_slugs, migrations.RunPython.noop),
        migrations.RunPython(backfill_board_slugs, migrations.RunPython.noop),
    ]
