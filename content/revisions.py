"""Revision recording and restore for the Content Studio history screen.

design_handoff_content_studio Phase 1.

⚠ **Call ``record_revision`` explicitly from the admin views. Never wire it to
a ``post_save`` signal.** A signal fires during migrations, during
``seed_content`` and ``_homepage_seed_data`` (which writes ~150 homepage rows
in a single run), and on the public site's own writes. The History screen would
fill with entries no human caused, and Undo would offer to "revert" a seed.

The snapshot is the row as it looked *before* the change, so restoring means
re-applying it. Restore never deletes history — it records a further revision,
which is what makes undo-of-an-undo work.
"""
import json

from django.contrib.contenttypes.models import ContentType
from django.core import serializers as dj_serializers
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction

from .models import ContentRevision


def snapshot_of(obj):
    """A JSON-safe dict of ``obj``'s concrete fields.

    Uses Django's own serializer so dates, decimals and file fields coerce the
    same way they do everywhere else — a hand-rolled ``__dict__`` copy trips
    over ``datetime`` and ``FieldFile`` the moment it hits ``JSONField``.
    """
    raw = dj_serializers.serialize("python", [obj])[0]["fields"]
    # serialize("python") leaves dates/Decimals as Python objects; the round
    # trip through DjangoJSONEncoder is what makes them JSONField-safe.
    return json.loads(json.dumps(raw, cls=DjangoJSONEncoder))


def record_revision(obj, action, actor=None, note="", snapshot=None):
    """Record what ``obj`` looked like before the caller's change.

    Pass ``snapshot`` when the object in memory has already been mutated — the
    caller is then responsible for having captured it first (see
    ``snapshot_before`` for the usual pattern).
    """
    if snapshot is None:
        snapshot = snapshot_of(obj)

    ct = ContentType.objects.get_for_model(obj.__class__)
    rev = ContentRevision.objects.create(
        content_type=ct,
        object_id=obj.pk,
        snapshot=snapshot,
        action=action,
        actor=actor if (actor and actor.is_authenticated) else None,
        note=note or "",
    )
    _prune(ct, obj.pk)
    return rev


def record_deletion(model, pk, snapshot, actor=None, note=""):
    """Record that a row was deleted, after it is already gone.

    ``record_revision`` reads ``obj.pk``, which is unusable here — the row no
    longer exists and Django may have cleared the attribute. The content
    type and id are passed explicitly so the feed can still say what was
    deleted and show its last state. ``restore_revision`` deliberately returns
    ``None`` for these: re-creating a deleted row would resurrect it under a
    new id and silently break anything that referenced the old one.
    """
    ct = ContentType.objects.get_for_model(model)
    rev = ContentRevision.objects.create(
        content_type=ct,
        object_id=pk,
        snapshot=snapshot or {},
        action=ContentRevision.ACTION_DELETED,
        actor=actor if (actor and actor.is_authenticated) else None,
        note=note or "",
    )
    _prune(ct, pk)
    return rev


def snapshot_before(obj):
    """Capture a snapshot to hand to ``record_revision`` after mutating ``obj``.

    Read a fresh copy from the database rather than trusting the in-memory
    instance, which a serializer may already have written over.
    """
    fresh = obj.__class__.objects.filter(pk=obj.pk).first()
    return snapshot_of(fresh) if fresh is not None else {}


def _prune(content_type, object_id):
    """Keep only the newest ``RETENTION_PER_OBJECT`` revisions for one object.

    Scoped per object, so a busy homepage row can never age out the history of
    a rarely-touched FAQ. ``BlogRevision`` is untouched by this — its unbounded
    retention is deliberate; see its docstring.
    """
    keep = ContentRevision.RETENTION_PER_OBJECT
    ids = list(
        ContentRevision.objects
        .filter(content_type=content_type, object_id=object_id)
        .order_by("-created_at", "-id")
        .values_list("id", flat=True)[keep:]
    )
    if ids:
        ContentRevision.objects.filter(id__in=ids).delete()


@transaction.atomic
def restore_revision(revision, actor=None):
    """Re-apply a snapshot onto its live row.

    Records a *new* revision of the pre-restore state first, so the restore is
    itself undoable. Returns the restored object, or ``None`` if the row it
    pointed at has since been deleted.
    """
    model = revision.content_type.model_class()
    obj = model.objects.filter(pk=revision.object_id).first()
    if obj is None:
        return None

    before = snapshot_of(obj)

    concrete = {f.name: f for f in model._meta.concrete_fields}
    for name, value in (revision.snapshot or {}).items():
        field = concrete.get(name)
        if field is None or field.primary_key:
            continue
        # Relations arrive as raw pk values from the serializer.
        setattr(obj, field.attname if field.is_relation else name, value)

    obj.save()

    # M2M separately, and after the save: ``concrete_fields`` above excludes
    # them, so a restore brought back a post's text and silently dropped its
    # tags. ``snapshot_of`` does capture them — Django's serializer emits a list
    # of pks — so the data was there all along and nothing read it.
    for field in model._meta.many_to_many:
        if field.name not in (revision.snapshot or {}):
            continue
        wanted = revision.snapshot[field.name] or []
        # A related row may have been deleted since the snapshot. Restoring the
        # survivors beats raising and abandoning the whole restore; the feed
        # still records what was re-applied.
        alive = list(
            field.remote_field.model.objects
            .filter(pk__in=wanted).values_list("pk", flat=True)
        )
        getattr(obj, field.name).set(alive)

    record_revision(
        obj,
        ContentRevision.ACTION_RESTORED,
        actor=actor,
        note=f"Restored the {revision.created_at:%-d %b} version",
        snapshot=before,
    )
    return obj
