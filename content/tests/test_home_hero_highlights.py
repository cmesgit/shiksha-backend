"""Tests for the homepage hero's "wire to real data" endpoint
(HomeHeroHighlightsView, /api/content/home/hero-highlights/).

The interesting cases are all about NOT counting or showing something that
isn't actually public-facing: a demo/seed account, an unstaffed teacher, a
draft course, a batch-scoped quiz nobody anonymous could ever see, or a
teacher's email address. Each test below pins one of those boundaries.

The teacher tests in particular pin a real inversion found on prod: gating
on an approved TeacherProfile + TEACHER role looked like the right
definition of "a real teacher" until it was checked against actual data —
every real, actively-teaching account on prod had neither, and every
academy-launch DEMO account was deliberately built to have neither either,
specifically so demo accounts stay off real-facing surfaces like chat
search. Profile+role was gating on "is this a demo account" by accident.
The real signals are an active TeachingAssignment (ground truth for "is
teaching something") and the seed email domain (ground truth for "is this
a demo account") — see SEED_EMAIL_DOMAIN in content/views.py.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Role, TeacherProfile, UserRole
from courses.models import (
    Batch, Board, Chapter, Course, Subject, TeachingAssignment,
)
from livestream.models import LiveSession
from materials.models import StudyMaterial
from quizzes.models import Quiz

User = get_user_model()


class HomeHeroHighlightsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.url = reverse("content:home-hero-highlights")
        self.role, _ = Role.objects.get_or_create(name="TEACHER")
        self.course = Course.objects.create(
            title="Class 10 Science", status=Course.STATUS_PUBLISHED,
            board=Board.objects.create(name="CBSE", board_type=Board.TYPE_CENTRAL),
            class_level=10,
        )
        self.subject = Subject.objects.create(course=self.course, name="Physics")

    def _teacher(self, username, *, approved=True, staffed=True, years=9,
                 subject="physics", full_name=""):
        user = User.objects.create_user(
            username=username, email=f"{username}@example.com", password="x",
            first_name=full_name,
        )
        UserRole.objects.create(user=user, role=self.role, is_active=True, is_primary=True)
        TeacherProfile.objects.create(
            user=user,
            academy_status=TeacherProfile.TRACK_APPROVED if approved else TeacherProfile.TRACK_PENDING,
            subject=subject,
            teaching_experience_years=years,
        )
        if staffed:
            # A fresh subject per teacher: TeachingAssignment has a unique
            # active constraint per (subject, batch), so staffing two
            # teachers onto the SAME course-wide subject would collide.
            own_subject = Subject.objects.create(course=self.course, name=f"{username}-subject")
            TeachingAssignment.objects.create(subject=own_subject, teacher=user, is_active=True)
        return user

    def test_anonymous_can_read(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertIn("stats", res.json())
        self.assertIn("feed", res.json())

    # ---- teacher count --------------------------------------------------

    def test_teacher_count_ignores_approval_status_but_requires_staffing(self):
        """Approval status is no longer the gate — a real pending teacher
        who is actually staffed on a subject counts; approval alone, with
        no staffing, does not."""
        self._teacher("approved_staffed", approved=True, staffed=True)
        self._teacher("pending_staffed", approved=False, staffed=True)
        self._teacher("approved_unstaffed", approved=True, staffed=False)
        stats = self.client.get(self.url).json()["stats"]
        self.assertEqual(stats["teachers"]["total"], 2)

    def test_teacher_count_excludes_seed_domain_regardless_of_activity(self):
        """The real prod inversion this pins: the academy-launch demo
        roster (real names, 1-24 active assignments each, zero profile or
        role) must NOT count as real teachers just because they look
        exactly like real, highly active ones. Excluded by email domain,
        not by profile/role — which is the one signal that actually
        distinguishes them."""
        real = self._teacher("real_one", full_name="Real Teacher")
        seed = User.objects.create_user(
            username="demo.teacher", email="demo.teacher@example.shikshacom.com",
            password="x", first_name="Demo", last_name="Teacher",
        )
        seed_subject = Subject.objects.create(course=self.course, name="Demo Subject")
        TeachingAssignment.objects.create(subject=seed_subject, teacher=seed, is_active=True)
        stats = self.client.get(self.url).json()["stats"]
        self.assertEqual(stats["teachers"]["total"], 1)
        feed = self.client.get(self.url).json()["feed"]
        self.assertFalse([i for i in feed if "Demo Teacher" in i["title"]])

    def test_teacher_counts_with_no_profile_or_role_at_all(self):
        """The exact shape of the real accounts found on prod: a real,
        named, actively-staffed person with NO TeacherProfile and NO
        TEACHER role — the profile/role bookkeeping lagging reality must
        not make them invisible."""
        user = User.objects.create_user(
            username="bare_but_real", email="bare_but_real@example.com",
            password="x", first_name="Bare", last_name="Teacher",
        )
        TeachingAssignment.objects.create(subject=self.subject, teacher=user, is_active=True)
        stats = self.client.get(self.url).json()["stats"]
        self.assertEqual(stats["teachers"]["total"], 1)
        feed = self.client.get(self.url).json()["feed"]
        self.assertTrue([i for i in feed if "Bare Teacher joined" == i["title"]])

    def test_teacher_count_not_inflated_by_multiple_assignments(self):
        """Two active TeachingAssignment rows for the same teacher must still
        count as ONE teacher — the exact join-duplication trap this
        codebase has hit before."""
        teacher = self._teacher("t1", staffed=False)
        other_subject = Subject.objects.create(course=self.course, name="Chemistry")
        TeachingAssignment.objects.create(subject=self.subject, teacher=teacher, is_active=True)
        TeachingAssignment.objects.create(subject=other_subject, teacher=teacher, is_active=True)
        stats = self.client.get(self.url).json()["stats"]
        self.assertEqual(stats["teachers"]["total"], 1)

    def test_teacher_count_excludes_inactive_assignment(self):
        teacher = self._teacher("inactive_t", staffed=False)
        TeachingAssignment.objects.create(subject=self.subject, teacher=teacher, is_active=False)
        stats = self.client.get(self.url).json()["stats"]
        self.assertEqual(stats["teachers"]["total"], 0)

    def test_teacher_new_count_respects_30_day_window(self):
        """"New" is keyed on the account's own date_joined — the only
        recency signal every real account reliably has, profile or not."""
        self._teacher("recent")
        old = self._teacher("old")
        User.objects.filter(pk=old.pk).update(date_joined=timezone.now() - timedelta(days=90))
        stats = self.client.get(self.url).json()["stats"]
        self.assertEqual(stats["teachers"]["total"], 2)
        self.assertEqual(stats["teachers"]["new_count"], 1)

    # ---- course count -----------------------------------------------------

    def test_course_count_excludes_draft_and_archived(self):
        Course.objects.create(title="Draft one", status=Course.STATUS_DRAFT)
        Course.objects.create(title="Archived one", status=Course.STATUS_ARCHIVED)
        Course.objects.create(title="Coming soon one", status=Course.STATUS_COMING_SOON)
        stats = self.client.get(self.url).json()["stats"]
        # self.course (PUBLISHED) + the COMING_SOON one.
        self.assertEqual(stats["courses"]["total"], 2)

    # ---- quiz count ---------------------------------------------------

    def test_quiz_count_excludes_unassigned_and_batch_scoped(self):
        Quiz.objects.create(subject=self.subject, title="Assigned, course-wide", is_assigned=True)
        Quiz.objects.create(subject=self.subject, title="Not assigned", is_assigned=False)
        batch = Batch.objects.create(course=self.course, name="2026-27", code="2026-27")
        batch_scoped = Quiz.objects.create(
            subject=self.subject, title="Assigned but batch-scoped", is_assigned=True,
        )
        batch_scoped.batches.add(batch)
        stats = self.client.get(self.url).json()["stats"]
        self.assertEqual(stats["quizzes"]["total"], 1)

    def test_quiz_count_excludes_quizzes_under_a_draft_course(self):
        draft_course = Course.objects.create(title="Draft course", status=Course.STATUS_DRAFT)
        draft_subject = Subject.objects.create(course=draft_course, name="Bio")
        Quiz.objects.create(subject=draft_subject, title="Hidden quiz", is_assigned=True)
        stats = self.client.get(self.url).json()["stats"]
        self.assertEqual(stats["quizzes"]["total"], 0)

    # ---- feed -----------------------------------------------------------

    def test_feed_includes_a_named_teacher_without_leaking_email(self):
        self._teacher("named", full_name="Priya Sharma")
        feed = self.client.get(self.url).json()["feed"]
        teacher_items = [i for i in feed if i["type"] == "new_teacher"]
        self.assertEqual(len(teacher_items), 1)
        self.assertIn("Priya Sharma", teacher_items[0]["title"])
        self.assertNotIn("@", teacher_items[0]["title"])
        self.assertNotIn("@", teacher_items[0]["subtitle"])

    def test_feed_omits_a_teacher_with_no_presentable_name(self):
        """No first/last name set is the realistic "nothing safe to show"
        case — must be skipped entirely, never fall back to the raw email
        OR the raw username. A real prod account (73 days old, no name set)
        surfaced publicly as "nili020903 joined" before this was fixed —
        a username is an identifier, not a name a visitor should be shown."""
        user = User.objects.create_user(
            username="bare", email="bare@example.com", password="x",
        )
        UserRole.objects.create(user=user, role=self.role, is_active=True, is_primary=True)
        TeacherProfile.objects.create(user=user, academy_status=TeacherProfile.TRACK_APPROVED)
        TeachingAssignment.objects.create(subject=self.subject, teacher=user, is_active=True)
        feed = self.client.get(self.url).json()["feed"]
        self.assertFalse([i for i in feed if i["type"] == "new_teacher"])
        for item in feed:
            self.assertNotIn("bare", item["title"] + item["subtitle"])

    def test_feed_excludes_stale_content_even_when_it_is_the_newest_of_its_type(self):
        """The real prod bug this pins: a low-activity subject can make an
        84-day-old row the ONLY/most-recent one it has. Without a recency
        bound, that surfaces in a "just published" feed as if it happened
        today — this is exactly how 84-day-old academy-launch seed rows
        ("— example quiz") showed up as current activity. An empty feed is
        the honest answer when nothing is actually recent."""
        stale_course = Course.objects.create(title="Stale Course", status=Course.STATUS_PUBLISHED)
        Course.objects.filter(pk=stale_course.pk).update(created_at=timezone.now() - timedelta(days=84))

        stale_subject = Subject.objects.create(course=stale_course, name="Stale Subject")
        stale_chapter = Chapter.objects.create(subject=stale_subject, title="Stale Chapter")
        stale_material = StudyMaterial.objects.create(
            subject=stale_subject, chapter=stale_chapter, title="Stale — example notes",
            uploaded_by=self._teacher("stale_uploader"),
        )
        StudyMaterial.objects.filter(pk=stale_material.pk).update(created_at=timezone.now() - timedelta(days=84))

        stale_quiz = Quiz.objects.create(subject=stale_subject, title="Stale — example quiz", is_assigned=True)
        Quiz.objects.filter(pk=stale_quiz.pk).update(created_at=timezone.now() - timedelta(days=84))

        stale_teacher = self._teacher("stale_teacher", staffed=False, full_name="Stale Teacher")
        TeachingAssignment.objects.create(subject=stale_subject, teacher=stale_teacher, is_active=True)
        User.objects.filter(pk=stale_teacher.pk).update(date_joined=timezone.now() - timedelta(days=84))

        feed = self.client.get(self.url).json()["feed"]
        titles = [i["title"] for i in feed]
        self.assertNotIn("Stale Course", titles)
        self.assertNotIn("Stale — example notes", titles)
        self.assertNotIn("Stale — example quiz", titles)
        self.assertNotIn("Stale Teacher joined", titles)

    def test_feed_includes_course_material_quiz_and_live_session(self):
        chapter = Chapter.objects.create(subject=self.subject, title="Kinematics")
        StudyMaterial.objects.create(
            subject=self.subject, chapter=chapter, title="Formula sheet",
            uploaded_by=self._teacher("uploader"),
        )
        Quiz.objects.create(subject=self.subject, title="Set 12", is_assigned=True)
        now = timezone.now()
        LiveSession.objects.create(
            course=self.course, subject=self.subject,
            title="Doubt session", start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(minutes=30), room_name="room_hero_test",
            created_by=self._teacher("live_host"), status=LiveSession.STATUS_LIVE,
        )
        feed = self.client.get(self.url).json()["feed"]
        types = {i["type"] for i in feed}
        self.assertIn("new_course", types)
        self.assertIn("new_material", types)
        self.assertIn("new_quiz", types)
        self.assertIn("live_session", types)

    def test_live_session_excludes_cancelled_and_completed(self):
        now = timezone.now()
        host = self._teacher("host2")
        for status, room in (
            (LiveSession.STATUS_CANCELLED, "room_cancelled"),
            (LiveSession.STATUS_COMPLETED, "room_completed"),
        ):
            LiveSession.objects.create(
                course=self.course, subject=self.subject, title="x",
                start_time=now - timedelta(minutes=5), end_time=now + timedelta(minutes=30),
                room_name=room, created_by=host, status=status,
            )
        feed = self.client.get(self.url).json()["feed"]
        self.assertFalse([i for i in feed if i["type"] == "live_session"])

    def test_feed_is_sorted_newest_first_and_capped(self):
        feed = self.client.get(self.url).json()["feed"]
        timestamps = [i["timestamp"] for i in feed]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    # ---- caching ----------------------------------------------------------

    def test_response_is_cached_across_requests(self):
        self._teacher("first")
        first = self.client.get(self.url).json()
        self.assertEqual(first["stats"]["teachers"]["total"], 1)

        self._teacher("second")
        still_cached = self.client.get(self.url).json()
        self.assertEqual(still_cached["stats"]["teachers"]["total"], 1)

        cache.clear()
        fresh = self.client.get(self.url).json()
        self.assertEqual(fresh["stats"]["teachers"]["total"], 2)
