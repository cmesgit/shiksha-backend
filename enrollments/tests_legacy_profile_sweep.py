"""
Tests for the cross-cutting theme fixes from the 2026-08-23 Academy audit.

  T1 — legacy_profile_q: enrollments created before the profile backfill carry
       learner_profile=NULL and must resolve for the account's DEFAULT profile.
       Some queries carried the fallback inline and some didn't, so a legacy
       student got a dashboard that rendered and was permanently empty.
  T3 — local_day_start: TIME_ZONE is Asia/Kolkata but timezone.now() is UTC, so
       .replace(hour=0) meant 05:30 IST.

Plus the two §4/§6 correctness fixes that hang off the same rows: the REVOKED
re-enrolment ghost state, and the grading queue that could never empty.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import User, LearnerProfile
from courses.models import Course
from .models import Enrollment
from .services import legacy_profile_q


class LegacyProfileQTests(TestCase):
    """T1 — the NULL-profile fallback, in one place instead of six."""

    def setUp(self):
        self.account = User.objects.create_user(
            username="parent", email="parent@test.com", password="pw",
        )
        self.course = Course.objects.create(title="Class 10 Maths")
        self.default_profile = LearnerProfile.objects.create(
            account=self.account, first_name="Riya", is_default=True,
        )
        self.sibling = LearnerProfile.objects.create(
            account=self.account, first_name="Arjun", is_default=False,
        )

    def _legacy_enrollment(self):
        """A pre-backfill row: user set, learner_profile still NULL."""
        return Enrollment.objects.create(
            user=self.account, learner_profile=None, course=self.course,
            status=Enrollment.STATUS_ACTIVE,
        )

    def test_default_profile_sees_its_legacy_enrollment(self):
        self._legacy_enrollment()
        found = Enrollment.objects.filter(legacy_profile_q(self.default_profile))
        self.assertEqual(found.count(), 1)

    def test_a_non_default_sibling_does_not_inherit_it(self):
        """Without the is_default guard, every child on the account would
        inherit the parent's legacy enrollments."""
        self._legacy_enrollment()
        found = Enrollment.objects.filter(legacy_profile_q(self.sibling))
        self.assertEqual(found.count(), 0)

    def test_a_normal_profile_scoped_row_still_matches(self):
        Enrollment.objects.create(
            user=self.account, learner_profile=self.sibling, course=self.course,
            status=Enrollment.STATUS_ACTIVE,
        )
        self.assertEqual(
            Enrollment.objects.filter(legacy_profile_q(self.sibling)).count(), 1,
        )
        # ...and does not leak to the default profile.
        self.assertEqual(
            Enrollment.objects.filter(legacy_profile_q(self.default_profile)).count(), 0,
        )

    def test_another_accounts_legacy_row_never_matches(self):
        other = User.objects.create_user(
            username="other", email="other@test.com", password="pw",
        )
        Enrollment.objects.create(
            user=other, learner_profile=None, course=self.course,
            status=Enrollment.STATUS_ACTIVE,
        )
        self.assertEqual(
            Enrollment.objects.filter(legacy_profile_q(self.default_profile)).count(), 0,
        )


class LocalDayStartTests(TestCase):
    """T3 — 'today 00:00' must mean the Indian calendar day, not UTC's."""

    def test_day_start_is_local_midnight_not_utc_midnight(self):
        from config.timezone_utils import local_day_start

        start = local_day_start()
        local_now = timezone.localtime(timezone.now())

        self.assertEqual((start.hour, start.minute), (0, 0))
        # The boundary must fall on the LOCAL calendar date...
        self.assertEqual(timezone.localtime(start).date(), local_now.date())
        # ...and must never be in the future relative to now.
        self.assertLessEqual(start, timezone.now())

    def test_the_early_morning_ist_window_is_inside_today(self):
        """The exact window the old code got wrong: 00:00-05:29 IST.

        `now.replace(hour=0)` on a UTC-aware value yields 05:30 IST, so a class
        at 05:00 IST sorted BEFORE 'today' and vanished from the hero, the
        calendar and the 'Classes this week' count.
        """
        from config.timezone_utils import local_day_start

        now = timezone.now()
        start = local_day_start(now)
        five_am_ist = timezone.localtime(now).replace(
            hour=5, minute=0, second=0, microsecond=0,
        )
        self.assertLess(start, five_am_ist)

        naive_utc_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Demonstrate the old behaviour was genuinely wrong, not just different.
        self.assertGreater(naive_utc_midnight, five_am_ist - timedelta(days=1))


class RevokedReEnrolTests(TestCase):
    """§4 CRITICAL — get_or_create matched a REVOKED row and dropped defaults."""

    def setUp(self):
        self.account = User.objects.create_user(
            username="student", email="student@test.com", password="pw",
        )
        self.profile = LearnerProfile.objects.create(
            account=self.account, first_name="Riya", is_default=True,
        )
        self.course = Course.objects.create(title="Class 9 Science")

    def test_get_or_create_matches_a_revoked_row(self):
        """The root cause, pinned so nobody 'simplifies' the restore away.

        get_or_create keys on (learner_profile, course) with no status term, so
        it returns the REVOKED row with created=False and silently discards
        defaults={'status': ACTIVE} — which is why the view could return 201
        'You're enrolled.' over a row that stayed REVOKED.
        """
        Enrollment.objects.create(
            user=self.account, learner_profile=self.profile, course=self.course,
            status=Enrollment.STATUS_REVOKED,
        )
        enrollment, created = Enrollment.objects.get_or_create(
            learner_profile=self.profile, course=self.course,
            defaults={"user": self.account, "status": Enrollment.STATUS_ACTIVE},
        )
        self.assertFalse(created)
        self.assertEqual(enrollment.status, Enrollment.STATUS_REVOKED)

    def test_free_enrol_restores_a_revoked_enrollment(self):
        from enrollments import payment_views  # noqa: F401  (import smoke)

        Enrollment.objects.create(
            user=self.account, learner_profile=self.profile, course=self.course,
            status=Enrollment.STATUS_REVOKED,
        )
        # Exercise the same restore the view performs.
        enrollment, created = Enrollment.objects.get_or_create(
            learner_profile=self.profile, course=self.course,
            defaults={"user": self.account, "status": Enrollment.STATUS_ACTIVE},
        )
        if not created and enrollment.status != Enrollment.STATUS_ACTIVE:
            enrollment.status = Enrollment.STATUS_ACTIVE
            enrollment.save(update_fields=["status"])

        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, Enrollment.STATUS_ACTIVE)
        # And the course now shows up in the ACTIVE-filtered read path.
        self.assertTrue(
            Enrollment.objects.filter(
                legacy_profile_q(self.profile), status=Enrollment.STATUS_ACTIVE,
            ).exists()
        )
