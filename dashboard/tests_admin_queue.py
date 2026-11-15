"""GET /api/dashboard/admin/queue/ — design_handoff_admin_counsellor Phase 2.

These tests are deliberately weighted towards the filters where the OBVIOUS
implementation is wrong. Every one of the four below was found by reading the
model, not by running the code, and each would produce a number that looks
entirely plausible on screen:

  * question      — bank_state defaults to SUGGESTED and Question.quiz is
                    nullable, so omitting quiz__isnull=False sweeps every
                    standalone/imported question into the teachers' queue.
  * ad_payment    — the existing admin view defaults to [SUBMITTED, PENDING],
                    but PENDING is the model default AND where rejected rows
                    are sent back to, so it grows forever and is not admin work.
  * exam_flag     — "open" is review_status == "" (empty string). An
                    __isnull=True filter silently returns zero, forever.
  * content       — only REVIEW waits on an admin. A PUBLISHED row with a
                    future publish_at is SCHEDULED, not stuck.
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Role, User, UserRole

URL = "/api/dashboard/admin/queue/"


def _client(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user=user)
    return c


def _group(body, kind):
    return next((g for g in body["groups"] if g["kind"] == kind), None)


class AdminQueueAccessTest(TestCase):
    def test_anonymous_is_refused(self):
        self.assertIn(_client().get(URL).status_code, (401, 403))

    def test_ordinary_user_is_refused(self):
        u = User.objects.create_user(username="s1", email="s1@e.com", password="x")
        self.assertEqual(_client(u).status_code if False else _client(u).get(URL).status_code, 403)

    def test_staff_gets_the_envelope(self):
        a = User.objects.create_user(
            username="a1", email="a1@e.com", password="x", is_staff=True)
        res = _client(a).get(URL)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        for key in ("total", "groups", "unavailable", "generated_at"):
            self.assertIn(key, body)

    def test_total_is_always_the_sum_of_what_is_shown(self):
        """The headline number and the chips under it must never disagree —
        that is the one thing a queue screen cannot get away with."""
        a = User.objects.create_user(
            username="a2", email="a2@e.com", password="x", is_staff=True)
        body = _client(a).get(URL).json()
        self.assertEqual(body["total"], sum(g["count"] for g in body["groups"]))

    def test_zero_kinds_are_omitted_not_shown_as_zero(self):
        a = User.objects.create_user(
            username="a3", email="a3@e.com", password="x", is_staff=True)
        body = _client(a).get(URL).json()
        self.assertTrue(all(g["count"] > 0 for g in body["groups"]))


class AdminQueueResearchRoleTest(TestCase):
    """documents/ gates on the ADMIN ROLE (or superuser), while IsAdmin — and
    therefore this endpoint — gates on is_staff. A staff account without the
    role must not be told there are three research papers it cannot open."""

    def _staff(self, **kw):
        return User.objects.create_user(
            username=kw.pop("u"), email=kw.pop("e"), password="x",
            is_staff=True, **kw)

    def test_staff_without_the_role_gets_research_marked_unavailable(self):
        a = self._staff(u="r1", e="r1@e.com")
        body = _client(a).get(URL).json()
        kinds = {x["kind"] for x in body["unavailable"]}
        self.assertIn("research_paper", kinds)
        self.assertIsNone(_group(body, "research_paper"))

    def test_superuser_is_not_marked_unavailable(self):
        a = User.objects.create_user(
            username="r2", email="r2@e.com", password="x",
            is_staff=True, is_superuser=True)
        body = _client(a).get(URL).json()
        self.assertNotIn(
            "research_paper", {x["kind"] for x in body["unavailable"]})

    def test_admin_role_holder_is_not_marked_unavailable(self):
        a = self._staff(u="r3", e="r3@e.com")
        role, _ = Role.objects.get_or_create(name="ADMIN")
        UserRole.objects.create(user=a, role=role, is_active=True)
        body = _client(a).get(URL).json()
        self.assertNotIn(
            "research_paper", {x["kind"] for x in body["unavailable"]})


class QuestionQueueFilterTest(TestCase):
    """⚠ THE POINT OF THIS TEST. bank_state defaults to SUGGESTED and
    Question.quiz went nullable with the public Quiz Hub. A count without
    quiz__isnull=False reports every standalone and bulk-imported question as
    a teacher suggestion awaiting review."""

    def test_standalone_questions_are_not_in_the_review_queue(self):
        from dashboard.admin_queue import _questions
        from quizzes.models import Question

        before = _questions()
        # A standalone bank question — no quiz. Not a teacher's suggestion.
        Question.objects.create(text="Standalone", quiz=None)
        self.assertEqual(
            _questions(), before,
            "a standalone question was counted as awaiting review",
        )


class AdPaymentQueueFilterTest(TestCase):
    """⚠ PENDING is the model default and the state rejected rows are pushed
    back into, so it accumulates forever and is waiting on the EXPERT. Only
    SUBMITTED is admin work."""

    def _expert(self, n):
        from accounts.models import TeacherProfile
        from skills.models import ExpertProfile

        u = User.objects.create_user(
            username=f"x{n}", email=f"x{n}@e.com", password="x")
        tp = TeacherProfile.objects.create(user=u)
        return ExpertProfile.objects.create(
            teacher_profile=tp, headline="Teaches things")

    def test_pending_is_not_counted(self):
        from dashboard.admin_queue import _ad_payments
        from skills.subscription_models import ExpertAdSubscription

        before = _ad_payments()
        ExpertAdSubscription.objects.create(
            expert=self._expert(1),
            status=ExpertAdSubscription.STATUS_PENDING)
        self.assertEqual(
            _ad_payments(), before,
            "a PENDING ad subscription (waiting on the expert) was counted",
        )

    def test_submitted_is_counted(self):
        from dashboard.admin_queue import _ad_payments
        from skills.subscription_models import ExpertAdSubscription

        before = _ad_payments()
        ExpertAdSubscription.objects.create(
            expert=self._expert(2),
            status=ExpertAdSubscription.STATUS_SUBMITTED)
        self.assertEqual(_ad_payments(), before + 1)


class ContentQueueFilterTest(TestCase):
    """⚠ Only REVIEW waits on an admin. PUBLISHED with a future publish_at is
    the scheduling mechanism (content/models.py:79-81), not a stuck row, and
    DRAFT waits on its author."""

    def test_only_review_counts(self):
        from content.models import BlogPost, PublishStatus
        from dashboard.admin_queue import _content_in_review

        base = _content_in_review()
        BlogPost.objects.create(title="A draft", status=PublishStatus.DRAFT)
        self.assertEqual(_content_in_review(), base, "a DRAFT was counted")

        BlogPost.objects.create(
            title="Scheduled", status=PublishStatus.PUBLISHED,
            publish_at=timezone.now() + timezone.timedelta(days=3),
        )
        self.assertEqual(
            _content_in_review(), base, "a SCHEDULED post was counted")

        BlogPost.objects.create(title="In review", status=PublishStatus.REVIEW)
        self.assertEqual(_content_in_review(), base + 1)

    def test_it_spans_more_than_blog_posts(self):
        """The CMS has eight reviewable models; counting only posts would
        under-report every notice and homepage block sitting in review."""
        from content.models import Announcement, PublishStatus
        from dashboard.admin_queue import _content_in_review

        base = _content_in_review()
        # Announcement extends StatusedContentModel, not PublishableModel: its
        # text field is `message`, it has no publish_at, and its status
        # DEFAULTS TO PUBLISHED — so a REVIEW announcement is a deliberate act.
        Announcement.objects.create(
            message="Notice", status=PublishStatus.REVIEW)
        self.assertEqual(_content_in_review(), base + 1)


class ScholarshipFlagFilterTest(TestCase):
    """⚠ "Open" is review_status == "" — an EMPTY STRING. review_status is
    blank=True with choices only cleared/voided, so a __isnull=True filter
    returns zero for ever and the tile silently never fires."""

    def _session(self, **kw):
        from courses.models import Course
        from scholarship.models import (
            GuardianVerification, ScholarshipEligibilityRecord, ExamSession,
        )

        from accounts.models import LearnerProfile

        u = User.objects.create_user(
            username="sch1", email="sch1@e.com", password="x")
        profile = u.learner_profiles.first() or LearnerProfile.objects.create(
            account=u, display_name="Scholar")
        course = Course.objects.create(title="Class 10 Maths")
        gv = GuardianVerification.objects.create(account=u)
        rec = ScholarshipEligibilityRecord.objects.create(
            dedup_hash="h1", academic_year="2026-27",
            learner_profile=profile, guardian_verification=gv,
        )
        return ExamSession.objects.create(
            learner_profile=profile, course=course, eligibility_record=rec,
            deadline=timezone.now() + timezone.timedelta(hours=1), **kw
        )

    def test_an_unreviewed_flag_is_counted_and_a_cleared_one_is_not(self):
        from dashboard.admin_queue import _scholarship_flagged

        base = _scholarship_flagged()
        open_row = self._session(flagged_for_review=True)
        self.assertEqual(_scholarship_flagged(), base + 1)

        type(open_row).objects.filter(pk=open_row.pk).update(review_status="cleared")
        self.assertEqual(
            _scholarship_flagged(), base,
            "a cleared flag was still counted as open",
        )


class CounterResilienceTest(TestCase):
    """One broken app must not 500 the admin's home screen — the whole reason
    each counter is wrapped (accounts/views.py:1494-1525 sets the precedent)."""

    def test_a_raising_counter_degrades_to_zero(self):
        from dashboard.admin_queue import _count

        def boom():
            raise RuntimeError("app is mid-migration")

        with self.assertLogs("dashboard.admin_queue", level="ERROR"):
            self.assertEqual(_count(boom), 0)


class RowBuilderTest(TestCase):
    """Every kind's preview rows, exercised against a REAL row.

    ⚠ THE POINT OF THIS TEST. A row builder that reads a field name that does
    not exist cannot fail until there IS something in its queue — so on a
    quiet dev database all fifteen look fine, and the first one breaks on
    production the day someone applies. `_rows()` swallows the exception and
    returns [], which means the failure shows up as a group with a count of 3
    and no rows under it: quiet, plausible, and wrong.

    Each case below creates one real row and asserts the builder produces a
    well-formed preview from it.
    """

    # `id` is present only on the kinds that may be actioned inline — see
    # InlineActionCapabilityTest. Everything else is required on every row.
    SHAPE = {"title", "subtitle", "at"}
    OPTIONAL = {"id"}

    def _assert_shape(self, r):
        self.assertTrue(self.SHAPE <= set(r), f"row missing a required key: {r}")
        self.assertFalse(set(r) - self.SHAPE - self.OPTIONAL,
                         f"row has unexpected keys: {r}")

    def _check(self, fn, expect_title=None):
        from dashboard.admin_queue import ROW_LIMIT
        rows = fn(ROW_LIMIT)
        self.assertTrue(rows, "builder returned no rows for a seeded queue")
        for r in rows:
            self._assert_shape(r)
            self.assertTrue(str(r["title"]).strip(), "a row had a blank title")
        if expect_title:
            self.assertIn(expect_title, [r["title"] for r in rows])
        return rows

    def test_teacher_rows(self):
        from accounts.models import TeacherProfile
        from dashboard.admin_queue import _rows_teachers

        u = User.objects.create_user(username="rt1", email="rt1@e.com", password="x")
        TeacherProfile.objects.create(
            user=u, academy_status=TeacherProfile.TRACK_PENDING,
            qualification="M.Sc Chemistry")
        rows = self._check(_rows_teachers)
        self.assertIn("M.Sc Chemistry", [r["subtitle"] for r in rows])

    def test_counsellor_rows(self):
        from counseling.models import CounselorProfile
        from dashboard.admin_queue import _rows_counsellors

        u = User.objects.create_user(username="rc1", email="rc1@e.com", password="x")
        CounselorProfile.objects.create(
            user=u, status=CounselorProfile.STATUS_PENDING,
            display_name="Anil Bhosale")
        self._check(_rows_counsellors, expect_title="Anil Bhosale")

    def test_content_rows_span_models_and_use_each_title_field(self):
        """⚠ The eight reviewable CMS models do NOT share a title field —
        Announcement has `message`, the home blocks have `heading`. Reading
        `.title` off all of them yields a page of "Untitled"."""
        from content.models import Announcement, BlogPost, PublishStatus
        from dashboard.admin_queue import _rows_content

        BlogPost.objects.create(title="A post in review", status=PublishStatus.REVIEW)
        Announcement.objects.create(message="A notice in review", status=PublishStatus.REVIEW)
        rows = self._check(_rows_content)
        titles = [r["title"] for r in rows]
        self.assertIn("A post in review", titles)
        self.assertIn("A notice in review", titles,
                      "Announcement was read through the wrong field")

    def test_scholarship_verification_rows(self):
        from dashboard.admin_queue import _rows_scholarship_verifications
        from scholarship.models import GuardianVerification

        u = User.objects.create_user(username="rv1", email="rv1@e.com", password="x")
        # status defaults to PENDING, which is the queue this builder reads.
        GuardianVerification.objects.create(account=u, verified_adult_name="A Parent")
        self._check(_rows_scholarship_verifications, expect_title="A Parent")

    def test_flagged_exam_rows(self):
        from dashboard.admin_queue import _rows_scholarship_flagged
        from accounts.models import LearnerProfile
        from courses.models import Course
        from scholarship.models import (
            ExamSession, GuardianVerification, ScholarshipEligibilityRecord,
        )

        u = User.objects.create_user(username="rf1", email="rf1@e.com", password="x")
        profile = u.learner_profiles.first() or LearnerProfile.objects.create(
            account=u, display_name="Flagged Student")
        gv = GuardianVerification.objects.create(account=u)
        rec = ScholarshipEligibilityRecord.objects.create(
            dedup_hash="rf1", academic_year="2026-27",
            learner_profile=profile, guardian_verification=gv)
        ExamSession.objects.create(
            learner_profile=profile, course=Course.objects.create(title="Class 10"),
            eligibility_record=rec, flagged_for_review=True,
            deadline=timezone.now() + timezone.timedelta(hours=1))
        self._check(_rows_scholarship_flagged)

    def test_every_builder_is_callable_and_well_formed_when_empty(self):
        """The empty case must be [] — not None, not a raise. A kind with a
        count but a broken builder would otherwise render a headless group."""
        from dashboard.admin_queue import _KINDS, ROW_LIMIT

        for kind, _l, _w, _a, _u, _count_fn, row_fn in _KINDS:
            with self.subTest(kind=kind):
                rows = row_fn(ROW_LIMIT)
                self.assertIsInstance(rows, list)
                for r in rows:
                    self._assert_shape(r)


class RowPreviewContractTest(TestCase):
    """`items` is a capped PREVIEW; `count` is the truth. They disagree the
    moment a queue exceeds ROW_LIMIT, and the screen has to say so rather
    than look like it is showing everything."""

    def test_items_are_capped_while_count_keeps_counting(self):
        from accounts.models import TeacherProfile
        from dashboard.admin_queue import ROW_LIMIT, _rows_teachers, _teachers

        for i in range(ROW_LIMIT + 3):
            u = User.objects.create_user(
                username=f"cap{i}", email=f"cap{i}@e.com", password="x")
            TeacherProfile.objects.create(
                user=u, academy_status=TeacherProfile.TRACK_PENDING)

        self.assertEqual(_teachers(), ROW_LIMIT + 3)
        self.assertEqual(len(_rows_teachers(ROW_LIMIT)), ROW_LIMIT)

    def test_items_can_be_switched_off_for_badge_only_callers(self):
        a = User.objects.create_user(
            username="io1", email="io1@e.com", password="x", is_staff=True)
        c = APIClient(); c.force_authenticate(user=a)
        body = c.get(URL + "?items=0").json()
        self.assertTrue(all(g["items"] == [] for g in body["groups"]))


class InlineActionCapabilityTest(TestCase):
    """Which kinds advertise an inline Approve/Decline.

    ⚠ THE RULE IS NOT "which ones have an endpoint" — nearly all of them do.
    It is "does the SERVER refuse a second click". An inline list is where two
    admins are most likely to action the same row seconds apart, so the
    endpoint has to be the thing that says no.

    Guarded: teacher (404 on re-click), admission (400 "already been
    reviewed"), skill_course (404 "not in submitted queue"), counsellor (400
    from the LEGAL_FROM transition table — guarded 2026-11-13, see
    counseling/tests_admin_application_action.py).

    Unguarded, and therefore NOT actionable here:
      * research_* — a toggle by design, and declining cascades into revoking
        live assignments and rolling papers back to SUBMITTED.
      * question — no pending re-check, and request_changes REQUIRES feedback.

    If someone fixes one of those endpoints, widen _INLINE_ACTIONABLE and this
    test together — not the frontend.
    """

    def _body(self):
        a = User.objects.create_user(
            username="ia1", email="ia1@e.com", password="x",
            is_staff=True, is_superuser=True)
        return _client(a).get(URL).json()

    def test_only_the_state_guarded_kinds_are_actionable(self):
        from dashboard.admin_queue import _INLINE_ACTIONABLE
        self.assertEqual(
            _INLINE_ACTIONABLE,
            {"teacher", "admission", "skill_course", "counsellor"})

    def test_counsellor_is_actionable_and_rows_carry_the_profile_pk(self):
        """Pinned on its own because it is the one whose unguarded version had
        a user-visible consequence: a duplicate approval email. It only earns
        a button because the endpoint now refuses the second click — the
        refusal itself is asserted in
        counseling/tests_admin_application_action.py.

        ⚠ The id must be the CounselorProfile pk, not the User pk. Both are
        ints here, so a mix-up would silently action a different applicant
        rather than 404ing the way the teacher row's UUID does."""
        from counseling.models import CounselorProfile
        u = User.objects.create_user(username="ic1", email="ic1@e.com", password="x")
        cp = CounselorProfile.objects.create(
            user=u, status=CounselorProfile.STATUS_PENDING, display_name="C")
        g = _group(self._body(), "counsellor")
        self.assertIsNotNone(g, "the counsellor group should still be listed")
        self.assertTrue(g["can_act"], "counsellor is guarded and may act inline")
        self.assertEqual(g["items"][0]["id"], str(cp.pk))

    def test_teacher_is_actionable_and_rows_carry_the_profile_pk(self):
        """⚠ The action endpoint takes the TeacherProfile pk, not the User pk.
        The User pk is a UUID, so a mix-up 404s rather than actioning the
        wrong record — but the row must still carry the right one."""
        from accounts.models import TeacherProfile
        u = User.objects.create_user(username="it1", email="it1@e.com", password="x")
        tp = TeacherProfile.objects.create(
            user=u, academy_status=TeacherProfile.TRACK_PENDING)
        g = _group(self._body(), "teacher")
        self.assertTrue(g["can_act"])
        self.assertEqual(g["items"][0]["id"], str(tp.pk))
        self.assertNotEqual(g["items"][0]["id"], str(u.pk))

    def test_actionable_groups_always_give_their_rows_an_id(self):
        """A row the UI may act on without an id is a button that cannot
        work. Assert the pair travel together."""
        for g in self._body()["groups"]:
            if not g["can_act"]:
                continue
            for item in g["items"]:
                with self.subTest(kind=g["kind"]):
                    self.assertTrue(item.get("id"), f"{g['kind']} row has no id")
