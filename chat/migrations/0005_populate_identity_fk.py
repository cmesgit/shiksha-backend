# PLACEMENT: backend/backend/chat/migrations/0005_populate_identity_fk.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/chat/migrations/0005_populate_identity_fk.py
#
# M1 data migration: backfill Participant.identity and Block.{blocker,
# blocked}_identity from the existing polymorphic (kind, learner_profile,
# teacher_profile) columns, matched against accounts.Identity by
# (kind_letter, profile_id). Depends on accounts.0022, which must have
# already populated the registry.
#
# profile_id is a CharField (str(pk) for either a UUID or an integer pk —
# see accounts/models.py's Identity docstring), so both sides of the lookup
# below are explicitly str()'d. Building identity_map from raw UUID/int
# objects and then looking it up with equally-raw objects would silently
# never match for the integer (TeacherProfile) side, since str-keyed and
# int-keyed dict entries don't compare equal even for "the same" value.
#
# Uses bulk_update rather than a per-row .save() loop — this table is the
# one most likely to be large in a real deployment (one row per participant
# per conversation), so a handful of batched UPDATEs matters more here than
# in the smaller accounts-side migration.
from django.db import migrations


def populate_identity_fks(apps, schema_editor):
    Participant = apps.get_model("chat", "Participant")
    Block = apps.get_model("chat", "Block")
    Identity = apps.get_model("accounts", "Identity")

    identity_map = {
        (i.kind, i.profile_id): i.id
        for i in Identity.objects.all().only("id", "kind", "profile_id")
    }

    def identity_id_for(kind, learner_id, teacher_id):
        if kind == "LEARNER" and learner_id:
            return identity_map.get(("L", str(learner_id)))
        if kind == "TEACHER" and teacher_id:
            return identity_map.get(("T", str(teacher_id)))
        return None

    to_update = []
    for p in Participant.objects.all().only(
        "id", "kind", "learner_profile_id", "teacher_profile_id"
    ).iterator():
        iid = identity_id_for(p.kind, p.learner_profile_id, p.teacher_profile_id)
        if iid:
            p.identity_id = iid
            to_update.append(p)
    if to_update:
        Participant.objects.bulk_update(to_update, ["identity_id"], batch_size=500)

    to_update = []
    for b in Block.objects.all().only(
        "id", "blocker_kind", "blocker_learner_id", "blocker_teacher_id",
        "blocked_kind", "blocked_learner_id", "blocked_teacher_id",
    ).iterator():
        changed = False
        blocker_iid = identity_id_for(b.blocker_kind, b.blocker_learner_id, b.blocker_teacher_id)
        blocked_iid = identity_id_for(b.blocked_kind, b.blocked_learner_id, b.blocked_teacher_id)
        if blocker_iid:
            b.blocker_identity_id = blocker_iid
            changed = True
        if blocked_iid:
            b.blocked_identity_id = blocked_iid
            changed = True
        if changed:
            to_update.append(b)
    if to_update:
        Block.objects.bulk_update(
            to_update, ["blocker_identity_id", "blocked_identity_id"], batch_size=500
        )


def clear_identity_fks(apps, schema_editor):
    """Reverse: safe — these FKs are a cache of the polymorphic columns,
    which remain the source of truth throughout M1."""
    Participant = apps.get_model("chat", "Participant")
    Block = apps.get_model("chat", "Block")
    Participant.objects.update(identity=None)
    Block.objects.update(blocker_identity=None, blocked_identity=None)


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0004_participant_block_identity"),
        ("accounts", "0022_populate_identity"),
    ]

    operations = [
        migrations.RunPython(populate_identity_fks, clear_identity_fks),
    ]
