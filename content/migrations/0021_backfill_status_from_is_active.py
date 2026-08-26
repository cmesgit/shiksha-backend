"""Backfill `status` from the legacy `is_active` boolean.

design_handoff_content_studio Phase 1.

0020's AddField gave every existing row `status="published"`, which is right
for the active ones and wrong for the rest — a row an editor had deliberately
taken down would come back as live. This corrects those.

Direction of truth here is is_active → status, because is_active is the only
signal that exists before this migration runs. Nothing is inferred as "draft":
a row cannot have been an unfinished draft under the old schema, since the old
schema had no way to express one. Everything is either published or archived.

Reverse restores the same relationship, so the pair is safely reversible.
"""
from django.db import migrations

# Every model that gained `status` in 0020.
MODELS = [
    "FAQItem",
    "Announcement",
    "ShowcaseCourse",
    "HomeContentBlock",
    "HomeListItem",
    "HomeFloater",
]


def is_active_to_status(apps, schema_editor):
    for name in MODELS:
        model = apps.get_model("content", name)
        # .update() bypasses save(), which is what we want inside a migration —
        # the model's reconcile logic isn't available on a historical model,
        # and these two writes are already consistent by construction.
        model.objects.filter(is_active=True).update(status="published")
        model.objects.filter(is_active=False).update(status="archived")


def status_to_is_active(apps, schema_editor):
    for name in MODELS:
        model = apps.get_model("content", name)
        model.objects.filter(status="published").update(is_active=True)
        model.objects.exclude(status="published").update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0020_announcement_status_faqitem_status_and_more"),
    ]

    operations = [
        migrations.RunPython(is_active_to_status, status_to_is_active),
    ]
