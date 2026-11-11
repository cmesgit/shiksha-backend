"""Restore chapter tabs for headings whose numeric prefix hid their kind."""

import re

from django.db import migrations


NUMBER = re.compile(r"^\s*\d+(?:\.\d+)*\s*[.):-]\s*")
KINDS = (
    ("worksheet", re.compile(r"^(?:worksheets?\b|activit(?:y|ies)\b|self[\s-]?assessment\b|exercise\b)", re.I)),
    ("action_plan", re.compile(r"^(?:action plan\b|my (?:career )?action plan\b|student action plan\b|my plan\b)", re.I)),
    ("references", re.compile(r"^references?\b", re.I)),
    ("faq", re.compile(r"^(?:faqs?\b|frequently asked questions?\b)", re.I)),
)


def classify_existing(apps, schema_editor):
    Chapter = apps.get_model("counseling", "GuideChapter")
    for chapter in Chapter.objects.filter(kind="content").only("id", "title").iterator():
        title = NUMBER.sub("", chapter.title).strip()
        for kind, pattern in KINDS:
            if pattern.search(title):
                Chapter.objects.filter(pk=chapter.pk).update(kind=kind)
                break


class Migration(migrations.Migration):
    dependencies = [("counseling", "0009_guidereadingprogress")]
    operations = [migrations.RunPython(classify_existing, migrations.RunPython.noop)]
