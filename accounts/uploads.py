"""Removing an upload that has been superseded.

Replacing a file only ever repointed the model field; the old object stayed
in the bucket forever. For KYC scans and application documents that is not
merely wasted storage — a blurred ID photo the applicant retook outlives the
one that replaced it, indefinitely, in a bucket that is publicly readable
(see the BunnyCDN finding). Nothing ever purged it.

Why this is deliberately narrow:

* **There is no bulk sweep, and there cannot be one.** `BunnyStorage`
  implements `_save`/`_open`/`exists`/`delete`/`url`/`size` and no
  `listdir`, so the objects in the zone cannot be enumerated through the
  storage API at all. Only files we are actively superseding can be found.

* **Deletion here is irreversible.** Media has no backup — the bucket is the
  only copy — and dev and prod SHARE one Edge Storage zone. A name still
  referenced by any row must therefore never be deleted, which is what
  `_still_referenced` exists for. It is not a nicety; it is the only thing
  standing between a re-save and someone else's document.

* It never raises. A storage backend that is down, or an object already
  gone, must not fail the applicant's form submission — the point of the
  save was to record their data, and the cleanup is housekeeping.
"""
import logging

from django.core.files.storage import default_storage
from django.db.models import Q

logger = logging.getLogger(__name__)

# Every model field that can hold one of these names. A name referenced by
# ANY of them is live and must survive. Keep this list complete: a field
# missing from it is a field whose documents this function can delete out
# from under.
_REFERENCING_FIELDS = (
    ("accounts.TeacherProfile", (
        "qualification_certificate", "id_proof_front", "id_proof_back",
        "signed_agreement", "skill_supporting_video", "skill_supporting_image",
        "photo",
    )),
    ("accounts.TeacherSkillApplication", ("supporting_file",)),
)


def _still_referenced(name):
    """True if any row anywhere still points at `name`."""
    from django.apps import apps

    for label, fields in _REFERENCING_FIELDS:
        model = apps.get_model(label)
        query = Q()
        for field in fields:
            query |= Q(**{field: name})
        if model.objects.filter(query).exists():
            return True
    return False


def discard_superseded_upload(name):
    """Delete a stored object that nothing points at any more.

    `name` is the OLD value of a file field, captured before it was
    reassigned. Call AFTER the new value has been saved, so that a failure
    between the two cannot leave the row pointing at a deleted object.

    Refuses — silently and deliberately — when the name is empty or still
    referenced.
    """
    if not name:
        return False
    if _still_referenced(name):
        # Two rows sharing one object. The skill-application carry-over can
        # produce this legitimately, and prod and dev share a bucket, so
        # "unreferenced here" is the strongest claim available.
        return False
    try:
        if default_storage.exists(name):
            default_storage.delete(name)
            logger.info("discarded superseded upload: %s", name)
            return True
    except Exception:
        # Housekeeping must never cost the applicant their submission.
        logger.warning("could not discard superseded upload: %s", name, exc_info=True)
    return False
