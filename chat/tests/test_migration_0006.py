# Permanent regression test for the exact proof this stage's brief demanded
# as its first task: migrate chat to 0005, insert a real legacy kind=COURSE
# row, apply 0006, assert survival as kind=ROOM/context_type="course"/
# context_id==old course_id, with the course_id property still working and
# the Participant untouched. Also covers the reverse direction.
#
# Uses TransactionTestCase + MigrationExecutor (the standard approach for
# testing a migration path against a real DB, not the final ORM state
# regular TestCase-based tests import chat.models against) — and is
# self-restoring (setUp/tearDown both migrate back to the latest leaf) so
# it can't leave the shared test DB mid-migration for every other test in
# this suite, regardless of test execution order or an assertion failure
# partway through.
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class Migration0006Test(TransactionTestCase):

    def setUp(self):
        super().setUp()
        self._restore_to_latest()

    def tearDown(self):
        self._restore_to_latest()
        super().tearDown()

    @staticmethod
    def _restore_to_latest():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_0005_to_0006_preserves_course_room_and_participant(self):
        # [1] Back to 0005.
        executor = MigrationExecutor(connection)
        executor.migrate([("chat", "0005_populate_identity_fk")])

        executor = MigrationExecutor(connection)
        state_0005 = executor.loader.project_state(("chat", "0005_populate_identity_fk"))
        OldConversation = state_0005.apps.get_model("chat", "Conversation")
        OldParticipant = state_0005.apps.get_model("chat", "Participant")
        OldUser = state_0005.apps.get_model("accounts", "User")
        OldLearnerProfile = state_0005.apps.get_model("accounts", "LearnerProfile")

        # [2] A real legacy kind=COURSE row + a Participant on it.
        old_course_id = uuid.uuid4()
        user = OldUser.objects.create(username="m0006_proof", email="m0006_proof@example.test")
        lp = OldLearnerProfile.objects.create(
            account=user, display_name="Proof Learner", relationship="SELF",
        )
        conv = OldConversation.objects.create(
            kind="COURSE", course_id=old_course_id, title="Physics 101",
        )
        participant = OldParticipant.objects.create(
            conversation=conv, kind="LEARNER", learner_profile=lp,
        )
        conv_pk, participant_pk, lp_pk = conv.pk, participant.pk, lp.pk

        # [3] Apply 0006.
        executor = MigrationExecutor(connection)
        executor.migrate([("chat", "0006_context_generalization")])

        executor = MigrationExecutor(connection)
        state_0006 = executor.loader.project_state(("chat", "0006_context_generalization"))
        NewConversation = state_0006.apps.get_model("chat", "Conversation")
        NewParticipant = state_0006.apps.get_model("chat", "Participant")

        # [4] Assert survival.
        new_conv = NewConversation.objects.get(pk=conv_pk)
        self.assertEqual(new_conv.kind, "ROOM")
        self.assertEqual(new_conv.context_type, "course")
        self.assertEqual(new_conv.context_id, str(old_course_id))
        self.assertEqual(new_conv.title, "Physics 101")

        new_participant = NewParticipant.objects.get(pk=participant_pk)
        self.assertEqual(new_participant.conversation_id, conv_pk)
        self.assertEqual(new_participant.learner_profile_id, lp_pk)

        # course_id is a @property, not migration state — only the real,
        # current chat.models.Conversation class has it.
        from chat.models import Conversation as RealConversation
        real_conv = RealConversation.objects.get(pk=conv_pk)
        self.assertEqual(real_conv.course_id, str(old_course_id))

    def test_0006_reverse_restores_course_id_and_kind(self):
        """Rolling back 0006 must restore kind=COURSE + course_id from
        context_type/context_id — a bad deploy shouldn't need a manual
        data fixup to roll back."""
        executor = MigrationExecutor(connection)
        executor.migrate([("chat", "0006_context_generalization")])

        executor = MigrationExecutor(connection)
        state_0006 = executor.loader.project_state(("chat", "0006_context_generalization"))
        Conversation6 = state_0006.apps.get_model("chat", "Conversation")

        course_id = uuid.uuid4()
        conv = Conversation6.objects.create(
            kind="ROOM", context_type="course", context_id=str(course_id),
            title="Chemistry 201",
        )
        conv_pk = conv.pk

        executor = MigrationExecutor(connection)
        executor.migrate([("chat", "0005_populate_identity_fk")])

        executor = MigrationExecutor(connection)
        state_0005 = executor.loader.project_state(("chat", "0005_populate_identity_fk"))
        Conversation5 = state_0005.apps.get_model("chat", "Conversation")

        old_conv = Conversation5.objects.get(pk=conv_pk)
        self.assertEqual(old_conv.kind, "COURSE")
        self.assertEqual(old_conv.course_id, course_id)
        self.assertEqual(old_conv.title, "Chemistry 201")

    def test_non_course_conversation_untouched(self):
        """A DIRECT conversation (no course_id at all) must pass through
        0006 with context_type/context_id left blank/null — the data
        migration only touches kind=COURSE rows."""
        executor = MigrationExecutor(connection)
        executor.migrate([("chat", "0005_populate_identity_fk")])

        executor = MigrationExecutor(connection)
        state_0005 = executor.loader.project_state(("chat", "0005_populate_identity_fk"))
        OldConversation = state_0005.apps.get_model("chat", "Conversation")
        conv = OldConversation.objects.create(kind="DIRECT", direct_key="L:1|T:2")
        conv_pk = conv.pk

        executor = MigrationExecutor(connection)
        executor.migrate([("chat", "0006_context_generalization")])

        executor = MigrationExecutor(connection)
        state_0006 = executor.loader.project_state(("chat", "0006_context_generalization"))
        NewConversation = state_0006.apps.get_model("chat", "Conversation")
        new_conv = NewConversation.objects.get(pk=conv_pk)
        self.assertEqual(new_conv.kind, "DIRECT")
        self.assertEqual(new_conv.context_type, "")
        self.assertIsNone(new_conv.context_id)
