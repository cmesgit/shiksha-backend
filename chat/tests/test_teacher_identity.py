# teacher_display_name() must publish the ACCOUNT HOLDER'S own identity,
# never a dependant's. See accounts/models.py's self_learner_profile()
# docstring for why default_learner_profile() is unsafe here: deleting the
# SELF profile is allowed, and the promotion that follows can make a CHILD
# profile the new default with no relationship filter. Same bug class as
# skills/tests_expert_identity_public.py — chat's teacher_display_name()
# was the one call site that pattern's original fix missed.
from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import LearnerProfile
from chat import services
from chat.tests.factories import make_teacher

User = get_user_model()


class TeacherDisplayNameIdentityTest(TestCase):
    def test_shows_the_account_holder_not_the_promoted_child(self):
        teacher = make_teacher()
        u = teacher.user
        # The account holder's own profile — not the default.
        LearnerProfile.objects.create(
            account=u, display_name="Parent Profile",
            first_name="Priya", last_name="Sharma",
            relationship=LearnerProfile.RELATIONSHIP_SELF,
            is_active=True, is_default=False,
        )
        # A dependant, promoted to default — the exact state a parent-
        # teacher reaches by deleting their own profile.
        LearnerProfile.objects.create(
            account=u, display_name="Child Profile",
            first_name="Aarav", last_name="Sharma",
            relationship=LearnerProfile.RELATIONSHIP_DEPENDENT,
            is_active=True, is_default=True,
        )

        name = services.teacher_display_name(teacher)

        self.assertEqual(name, "Priya Sharma")
        self.assertNotIn("Aarav", name)

    def test_falls_back_to_username_when_no_self_profile_exists(self):
        teacher = make_teacher()
        LearnerProfile.objects.create(
            account=teacher.user, display_name="Only Child",
            first_name="Aarav", last_name="Sharma",
            relationship=LearnerProfile.RELATIONSHIP_DEPENDENT,
            is_active=True, is_default=True,
        )

        name = services.teacher_display_name(teacher)

        self.assertNotIn("Aarav", name)
        self.assertEqual(name, teacher.user.username or teacher.user.email)
