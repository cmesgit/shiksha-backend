"""Backfill every existing Document into the set shape.

0010 added DocumentFile and DocumentTopic. Until this runs, every document on
dev and prod is a set with zero files and zero tags — which is not "empty", it
is invisible: the redesigned Files list renders nothing and the browse facets
have no subject to group it under.

Two rules this migration follows, both learned the hard way in this codebase:

1. **A data migration must not depend on the app's own modules.** `field_for_
   subject` is duplicated below rather than imported from `documents.fields`,
   because a migration runs against whatever that file says at the time, not
   what it said when the migration was written. Changing the field map later
   must not retroactively change what this migration did.

2. **Reversing must undo exactly what this added, and nothing else.** The
   reverse deletes only the rows this created, identified by `order=0` on
   documents that still have a single file — it does not truncate the tables,
   because by then a real multi-file upload may already exist.
"""
from django.db import migrations

# Frozen copy of documents/fields.py's SUBJECT_FIELDS at the time of writing.
# Do not "fix" this to an import. See rule 1 above.
_SUBJECT_FIELDS = {
    "Civil Engineering": "Engineering",
    "Mechanical Engineering": "Engineering",
    "Electrical Engineering": "Engineering",
    "Chemical Engineering": "Engineering",
    "Computer Science": "Engineering",
    "Electronics": "Engineering",
    "Architecture": "Engineering",
    "Physics": "Sciences",
    "Chemistry": "Sciences",
    "Biology": "Sciences",
    "Mathematics": "Sciences",
    "Statistics": "Sciences",
    "Environmental Science": "Sciences",
    "Agriculture": "Sciences",
    "Economics": "Social Sciences",
    "Political Science": "Social Sciences",
    "Sociology": "Social Sciences",
    "Psychology": "Social Sciences",
    "Geography": "Social Sciences",
    "Law": "Social Sciences",
    "Social Work": "Social Sciences",
    "Education": "Social Sciences",
    "History": "Humanities",
    "Philosophy": "Humanities",
    "English": "Humanities",
    "Languages": "Humanities",
    "Fine Arts": "Humanities",
    "Media and Communication": "Humanities",
    "Commerce": "Commerce",
    "Accounting": "Commerce",
    "Finance": "Commerce",
    "Management": "Commerce",
    "Competitive Exams": "Exam Prep",
    "Medicine": "Health",
    "Nursing": "Health",
    "Pharmacy": "Health",
    "General": "Other",
    "Skill Development": "Other",
    "Sports and Physical Education": "Other",
}
_BY_CASEFOLD = {s.casefold(): f for s, f in _SUBJECT_FIELDS.items()}
_UNFILED = "Other"


def _field_for(subject):
    if not subject:
        return _UNFILED
    return _BY_CASEFOLD.get(subject.strip().casefold(), _UNFILED)


def forwards(apps, schema_editor):
    Document = apps.get_model("documents", "Document")
    DocumentFile = apps.get_model("documents", "DocumentFile")
    DocumentTopic = apps.get_model("documents", "DocumentTopic")

    files, topics = [], []

    # .iterator() because prod's Explore library is unbounded and this would
    # otherwise load every row before writing one.
    for doc in Document.objects.all().iterator(chunk_size=500):
        # A document with no file is legitimate — `Document.file` is
        # null=True and the reader renders `full` instead. Those sets get a
        # tag but no DocumentFile, which is correct: zero files, not a row
        # pointing at nothing.
        if doc.file:
            files.append(DocumentFile(
                document_id=doc.pk,
                owner_id=doc.owner_id,
                is_removed=doc.is_removed,
                file=doc.file,
                # `file.name` is the stored path; the basename is the closest
                # thing to the uploader's own filename that survives. Better
                # than blank, which the Files list would render as an unnamed
                # row.
                original_name=str(doc.file).rsplit("/", 1)[-1],
                filetype=doc.filetype or "",
                size=0,  # not knowable without touching storage; see note below
                pages=doc.pages or 0,
                file_hash=doc.file_hash or "",
                order=0,
            ))

        subject = (doc.subject or "").strip()
        if subject:
            topics.append(DocumentTopic(
                document_id=doc.pk,
                field=_field_for(subject),
                subject=subject,
                branch=(doc.topic or "").strip(),
                order=0,
            ))

    # Not bulk_create(ignore_conflicts=True): a conflict here would mean this
    # migration has already run, and silently skipping would leave a partial
    # backfill looking like a complete one.
    DocumentFile.objects.bulk_create(files, batch_size=500)
    DocumentTopic.objects.bulk_create(topics, batch_size=500)


def backwards(apps, schema_editor):
    DocumentFile = apps.get_model("documents", "DocumentFile")
    DocumentTopic = apps.get_model("documents", "DocumentTopic")
    # Only the rows this migration could have made. A set that has since
    # gained a second file keeps both — unapplying the backfill must not
    # destroy an upload made after it.
    DocumentFile.objects.filter(order=0).delete()
    DocumentTopic.objects.filter(order=0).delete()


class Migration(migrations.Migration):
    """`size` is left at 0 for backfilled files on purpose.

    Reading it means a storage round trip per file, and on prod that storage is
    BunnyCDN over the network — a migration that makes one HTTP request per
    document is a migration that times out halfway through and leaves the
    library half-converted. The redesign shows a size per file; backfilled rows
    will show nothing there until the file is next touched. A separate
    management command can fill them in at its own pace, out of the deploy's
    critical path.
    """

    dependencies = [
        ("documents", "0010_documentfile_documenttopic"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
