"""Keep every Document's set children populated, on every write path.

Migration 0011 backfilled the documents that existed when it ran. That is
exactly half the problem: a document created *after* it — by an upload, by
Research Hub publishing a paper, or by someone in the Django admin — would be
born with no `DocumentFile` and no `DocumentTopic`, and render as an empty set
that no browse facet can see. The failure is silent at write time and only
visible much later, on a page nobody was looking at.

So this is derived data that has to stay correct on every write path, which is
the one case where a signal is the right tool in this codebase — the same
reasoning as `content/media_signals.py`, and the opposite of `ContentRevision`,
where a signal would let a migration forge a human action.

⚠ BACKFILL-ONLY, NEVER A REWRITE. This fills children in when a set has none.
It must not keep them in step with `Document.file` / `subject` / `topic`
afterwards, because from Phase 8 those parent columns hold only the *first* of
several files and the *first* of several tags. A version of this that rewrote
on every save would delete the second file of every multi-file upload.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .fields import field_for_subject
from .models import Document, DocumentFile, DocumentTopic


@receiver(post_save, sender=Document, dispatch_uid="documents_sync_set_children")
def sync_set_children(sender, instance, **kwargs):
    doc = instance
    fields = kwargs.get("update_fields")

    # ── Propagate removal ────────────────────────────────────────────────
    # This has to run even for a targeted save, because every removal path is
    # a targeted save: DocumentDeleteView and moderation's _remove_document
    # both call save(update_fields=[..., "is_removed", ...]).
    #
    # ⚠ Not optional bookkeeping. `DocumentFile.is_removed` is half of the
    # duplicate-upload constraint, and the constraint only ignores a row when
    # that flag is True. Leave it stale and deleting a document no longer frees
    # its file: the owner can never re-upload a corrected version, and the
    # error they get names a document they can no longer see.
    if fields is None or "is_removed" in fields:
        DocumentFile.objects.filter(document=doc).exclude(
            is_removed=doc.is_removed,
        ).update(is_removed=doc.is_removed)

    # ── Backfill children ────────────────────────────────────────────────
    # Everything below creates rows, and no targeted save can need it — those
    # callers are flipping a flag or refreshing a counter on a set that already
    # exists. Returning here keeps two queries off a hot path.
    if fields:
        return

    # `.exists()` rather than `.count()`: the question is "has this set been
    # populated", and a set with ten files should not cost ten rows to answer.
    if doc.file and not DocumentFile.objects.filter(document=doc).exists():
        DocumentFile.objects.create(
            document=doc,
            # owner / is_removed are set from the parent in
            # DocumentFile.save(); passing them here would be a second source
            # of truth for the duplicate-upload constraint.
            owner_id=doc.owner_id,
            file=doc.file,
            original_name=str(doc.file).rsplit("/", 1)[-1],
            filetype=doc.filetype or "",
            pages=doc.pages or 0,
            file_hash=doc.file_hash or "",
            order=0,
        )

    subject = (doc.subject or "").strip()
    if subject and not DocumentTopic.objects.filter(document=doc).exists():
        DocumentTopic.objects.create(
            document=doc,
            field=field_for_subject(subject),
            subject=subject,
            branch=(doc.topic or "").strip(),
            order=0,
        )
