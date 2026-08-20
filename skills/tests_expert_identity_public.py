# The PUBLIC expert directory must show the account holder, not their child —
# and must not cost one query per row to do it.
#
# self_learner_profile() was introduced for exactly this, but four call sites
# that resolve a public identity were missed, including the one that falls
# through to the profile PHOTO: a dependant's face could become an expert's
# avatar on a directory anyone can browse without logging in.

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from rest_framework.test import APIClient

from accounts.models import LearnerProfile, Role, TeacherProfile, UserRole
from skills.models import ExpertProfile

User = get_user_model()


class PublicExpertIdentityTests(TestCase):
    def setUp(self):
        role, _ = Role.objects.get_or_create(name="TEACHER")
        self.experts = []
        for i in range(6):
            u = User.objects.create_user(
                username=f"pe{i}@x.com", email=f"pe{i}@x.com", password="x")
            UserRole.objects.create(user=u, role=role, is_active=True,
                                    is_primary=True)
            # The account holder's own profile — NOT the default.
            LearnerProfile.objects.create(
                account=u, display_name=f"Parent{i}", full_name=f"Parent {i}",
                student_id=f"PE{i}", relationship="SELF", is_active=True,
                is_default=False)
            # A child, and it IS the default — the exact state a parent
            # reaches by deleting their own profile and having the promotion
            # pick a dependant.
            LearnerProfile.objects.create(
                account=u, display_name=f"Child{i}", full_name=f"Child {i}",
                student_id=f"PEC{i}", relationship="CHILD", is_active=True,
                is_default=True)
            tp = TeacherProfile.objects.create(user=u)
            self.experts.append(ExpertProfile.objects.create(
                teacher_profile=tp, headline=f"Tutor {i}", is_listed=True))

    def test_directory_shows_the_account_holder_not_the_child(self):
        r = APIClient().get("/api/skill/student/experts/")
        self.assertEqual(r.status_code, 200, r.content)
        names = " ".join(e["name"] for e in r.json())
        self.assertIn("Parent", names)
        self.assertNotIn(
            "Child", names,
            "a dependant's name is being published as the expert's identity")

    def _query_count(self):
        client = APIClient()
        client.get("/api/skill/student/experts/")  # warm any one-off caches
        with CaptureQueriesContext(connection) as ctx:
            client.get("/api/skill/student/experts/")
        return len(ctx.captured_queries)

    def test_listing_cost_does_not_grow_with_the_number_of_experts(self):
        """This page is public and unauthenticated, and it had THREE separate
        per-row queries: the identity lookup, a COUNT for review totals, and —
        the big one — the GlobalSettings singleton reloaded by is_advertised(),
        which the sort called twice per expert and the payload a third time.
        Six experts cost 27 queries; thirty cost 34.

        Asserting flatness rather than a magic number: the failure mode is
        growth, and a fixed ceiling silently tolerates it until the ceiling is
        crossed."""
        before = self._query_count()

        role = Role.objects.get(name="TEACHER")
        for i in range(20):
            u = User.objects.create_user(
                username=f"pe_x{i}@x.com", email=f"pe_x{i}@x.com", password="x")
            UserRole.objects.create(user=u, role=role, is_active=True,
                                    is_primary=True)
            LearnerProfile.objects.create(
                account=u, display_name=f"X{i}", full_name=f"X {i}",
                student_id=f"PEX{i}", relationship="SELF", is_active=True,
                is_default=True)
            tp = TeacherProfile.objects.create(user=u)
            ExpertProfile.objects.create(teacher_profile=tp,
                                         headline=f"X{i}", is_listed=True)

        after = self._query_count()
        self.assertEqual(
            before, after,
            f"query count grew with the expert count ({before} → {after}) — "
            f"something in the row loop is hitting the DB per expert")

    def test_prefetched_and_unprefetched_agree(self):
        """The prefetch fast-path must return exactly what the query path
        returns — a divergence here would be invisible and wrong."""
        u = self.experts[0].user
        plain = User.objects.get(pk=u.pk).self_learner_profile()
        prefetched = (User.objects.prefetch_related("learner_profiles")
                      .get(pk=u.pk).self_learner_profile())
        self.assertEqual(plain.id, prefetched.id)
        self.assertEqual(plain.relationship, "SELF")
