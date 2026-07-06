# M1 regression (Phase 3 §6/§7) — re-verified as part of M3 per this
# stage's "full M0/M1/M2 regression" requirement. No suite for this
# existed in the codebase before this stage; written fresh here.
from django.test import TestCase

from accounts.models import Identity
from chat import services
from chat.models import Participant

from .factories import make_learner, make_teacher


class IdentityDualWriteTest(TestCase):
    """services._attach_participant() / create_block() must populate the
    identity/blocker_identity/blocked_identity FKs restored in this
    stage's baseline fix (see chat/models.py's docstring on those
    fields) — this is the exact functionality that drift would have
    broken (a TypeError on the very first _attach_participant() call)."""

    def test_attach_participant_learner_sets_identity_fk(self):
        lp = make_learner()
        conv = services.ensure_room("course", "ctx-1", title="Room")
        participant = services._attach_participant(conv, Participant.KIND_LEARNER, lp)

        self.assertIsNotNone(participant.identity_id)
        identity = Identity.objects.get(kind=Identity.KIND_LEARNER, profile_id=str(lp.id))
        self.assertEqual(participant.identity_id, identity.id)

    def test_attach_participant_teacher_sets_identity_fk(self):
        tp = make_teacher()
        conv = services.ensure_room("course", "ctx-2", title="Room")
        participant = services._attach_participant(conv, Participant.KIND_TEACHER, tp)

        self.assertIsNotNone(participant.identity_id)
        identity = Identity.objects.get(kind=Identity.KIND_TEACHER, profile_id=str(tp.id))
        self.assertEqual(participant.identity_id, identity.id)

    def test_attach_participant_defaults_only_apply_on_create(self):
        """Re-attaching the same identity to the same conversation must not
        error and must not duplicate the Participant row (get_or_create's
        `defaults` only apply on insert — this just confirms the second
        call is a safe no-op, per the function's own docstring)."""
        lp = make_learner()
        conv = services.ensure_room("course", "ctx-3", title="Room")
        first = services._attach_participant(conv, Participant.KIND_LEARNER, lp)
        second = services._attach_participant(conv, Participant.KIND_LEARNER, lp)
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            Participant.objects.filter(conversation=conv, learner_profile=lp).count(), 1,
        )

    def test_create_block_sets_both_identity_fks(self):
        teacher = make_teacher()
        learner = make_learner()
        block = services.create_block(
            Participant.KIND_TEACHER, teacher, Participant.KIND_LEARNER, learner,
        )
        teacher_identity = Identity.objects.get(kind=Identity.KIND_TEACHER, profile_id=str(teacher.id))
        learner_identity = Identity.objects.get(kind=Identity.KIND_LEARNER, profile_id=str(learner.id))
        self.assertEqual(block.blocker_identity_id, teacher_identity.id)
        self.assertEqual(block.blocked_identity_id, learner_identity.id)


class IdentityClaimFastPathTest(TestCase):
    """active_identity_from_claims()'s M1 dual-read: the identity_claim
    fast path, with a fall-through to the legacy context/active_profile
    path on ANY failure — absent, malformed, or fails ownership check."""

    def test_valid_identity_claim_resolves_learner(self):
        lp = make_learner()
        kind, obj = services.active_identity_from_claims(
            lp.account, context=None, active_profile_id=None,
            identity_claim=f"L:{lp.id}",
        )
        self.assertEqual(kind, Participant.KIND_LEARNER)
        self.assertEqual(obj.id, lp.id)

    def test_identity_claim_for_a_different_account_falls_through(self):
        lp = make_learner()
        stranger = make_learner().account  # a different account entirely
        kind, obj = services.active_identity_from_claims(
            stranger, context=None, active_profile_id=None,
            identity_claim=f"L:{lp.id}",  # doesn't belong to `stranger`
        )
        # No legacy context given either, so this must resolve to nothing —
        # NOT to lp, which would be a cross-account identity leak.
        self.assertIsNone(kind)
        self.assertIsNone(obj)

    def test_malformed_identity_claim_falls_back_to_legacy_context(self):
        lp = make_learner()
        kind, obj = services.active_identity_from_claims(
            lp.account, context="learner", active_profile_id=str(lp.id),
            identity_claim="not-a-real-claim",
        )
        self.assertEqual(kind, Participant.KIND_LEARNER)
        self.assertEqual(obj.id, lp.id)

    def test_teacher_identity_claim(self):
        tp = make_teacher()
        kind, obj = services.active_identity_from_claims(
            tp.user, context=None, active_profile_id=None,
            identity_claim=f"T:{tp.id}",
        )
        self.assertEqual(kind, Participant.KIND_TEACHER)
        self.assertEqual(obj.id, tp.id)
