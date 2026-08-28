"""Teacher roster endpoints: one row per STUDENT, not per account.

Regression cover for the multi-profile bug in ``TeacherAllStudentsView`` /
``SubjectStudentsView``: both used to emit ``id = User.id`` and the all-students
view deduped on it, so a parent with three enrolled children collapsed into a
single row identified by the family account rather than the student.
"""

from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import LearnerProfile, Role, User, UserRole
from enrollments.models import Enrollment

from rest_framework.test import APIClient

from .models import Batch, Board, Chapter, Course, Subject, TeachingAssignment

ALL_STUDENTS_URL = "/api/courses/teacher/all-students/"


class TeacherRosterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)

        cls.teacher = User.objects.create_user(
            username="teach", email="teach@example.com", password="pw",
        )
        UserRole.objects.create(user=cls.teacher, role=teacher_role)

        cls.course = Course.objects.create(title="Class 10 Science")
        cls.other_course = Course.objects.create(title="Class 10 Maths")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")
        cls.other_subject = Subject.objects.create(
            course=cls.other_course, name="Algebra",
        )
        # The teacher teaches one subject in each course, so both courses are
        # in scope for the all-students view.
        TeachingAssignment.objects.create(subject=cls.subject, teacher=cls.teacher, batch=None, is_active=True)
        TeachingAssignment.objects.create(subject=cls.other_subject, teacher=cls.teacher, batch=None, is_active=True)

        # ── One account, three enrolled children: 1 user, 3 students. ──
        cls.parent = User.objects.create_user(
            username="parent", email="parent@example.com", password="pw",
        )
        cls.children = [
            LearnerProfile.objects.create(
                account=cls.parent,
                display_name=name,
                full_name=name,
                relationship="SON",
                is_default=(name == "Aaron Doe"),
            )
            for name in ("Aaron Doe", "Bina Doe", "Chandan Doe")
        ]
        for child in cls.children:
            Enrollment.objects.create(
                user=cls.parent,
                learner_profile=child,
                course=cls.course,
                status=Enrollment.STATUS_ACTIVE,
            )

        # ── A legacy enrollment: learner_profile is NULL, so it must be
        # attributed to the account's DEFAULT profile (the _dual_key_q rule). ──
        cls.legacy_account = User.objects.create_user(
            username="legacy", email="legacy@example.com", password="pw",
        )
        cls.legacy_default = LearnerProfile.objects.create(
            account=cls.legacy_account,
            display_name="Legacy Learner",
            full_name="Legacy Learner",
            is_default=True,
        )
        # A non-default sibling that must NOT absorb the legacy row.
        cls.legacy_sibling = LearnerProfile.objects.create(
            account=cls.legacy_account,
            display_name="Younger Sibling",
            full_name="Younger Sibling",
            relationship="SON",
        )
        Enrollment.objects.create(
            user=cls.legacy_account,
            learner_profile=None,
            course=cls.course,
            status=Enrollment.STATUS_ACTIVE,
        )

    def setUp(self):
        # DRF is configured with CookieJWTAuthentication only (no session auth),
        # so force_login() would leave the request anonymous. The teacher
        # roster views require an active TEACHER-CONTEXT token, not just the
        # role (see require_teacher_context/_in_teacher_context) — a bare
        # RefreshToken.for_user() carries no context claim at all, so it must
        # be set explicitly here to match what a real teacher session token
        # looks like (accounts.auth_flow.build_tokens sets this the same way).
        refresh = RefreshToken.for_user(self.teacher)
        refresh["context"] = "teacher"
        self.client.cookies["access"] = str(refresh.access_token)

    # ───────────────────────────── all students ─────────────────────────────
    def test_siblings_are_separate_rows_keyed_on_the_learner_profile(self):
        rows = self.client.get(ALL_STUDENTS_URL).json()["students"]

        family = [r for r in rows if r["email"] == "parent@example.com"]
        self.assertEqual(len(family), 3, "each enrolled child must get its own row")
        self.assertEqual(
            {r["id"] for r in family},
            {str(c.id) for c in self.children},
            "row id must be the learner-profile id, not the account id",
        )
        # The account is still reported, just not as the student's identity.
        self.assertEqual({r["account_id"] for r in family}, {str(self.parent.id)})
        self.assertNotIn(str(self.parent.id), {r["id"] for r in rows})

    def test_legacy_null_profile_row_resolves_to_the_default_profile(self):
        rows = self.client.get(ALL_STUDENTS_URL).json()["students"]

        legacy = [r for r in rows if r["email"] == "legacy@example.com"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["id"], str(self.legacy_default.id))
        self.assertFalse(legacy[0]["unresolved_profile"])
        self.assertNotIn(
            str(self.legacy_sibling.id),
            {r["id"] for r in rows},
            "a non-default sibling must not be credited with a legacy enrollment",
        )

    def test_total_students_counts_students_not_accounts(self):
        body = self.client.get(ALL_STUDENTS_URL).json()
        # 3 children + 1 legacy student, from only 2 accounts.
        self.assertEqual(body["total_students"], 4)
        self.assertEqual(len(body["students"]), 4)

    def test_one_student_in_two_of_the_teachers_courses_is_one_row(self):
        Enrollment.objects.create(
            user=self.parent,
            learner_profile=self.children[0],
            course=self.other_course,
            status=Enrollment.STATUS_ACTIVE,
        )
        rows = self.client.get(ALL_STUDENTS_URL).json()["students"]

        matching = [r for r in rows if r["id"] == str(self.children[0].id)]
        self.assertEqual(len(matching), 1, "dedupe is per student, per roster")
        self.assertEqual(
            sorted(matching[0]["course_titles"]),
            ["Class 10 Maths", "Class 10 Science"],
            "every shared course is listed, not just whichever came first",
        )

    def test_rows_are_sorted_by_name_including_legacy_rows(self):
        rows = self.client.get(ALL_STUDENTS_URL).json()["students"]
        names = [r["full_name"] for r in rows]
        self.assertEqual(names, sorted(names, key=str.lower))
        # Legacy rows sort by their resolved profile's name, not by NULL.
        self.assertIn("Legacy Learner", names)

    # ─────────────────────────── subject students ───────────────────────────
    def test_subject_students_are_keyed_on_the_learner_profile(self):
        url = f"/api/courses/subjects/{self.subject.id}/students/"
        body = self.client.get(url).json()

        self.assertEqual(body["total_students"], 4)
        ids = {r["id"] for r in body["students"]}
        self.assertEqual(
            ids,
            {str(c.id) for c in self.children} | {str(self.legacy_default.id)},
        )
        self.assertNotIn(str(self.parent.id), ids)

    def test_subject_students_dedupes_a_legacy_and_migrated_pair(self):
        """unique_together("learner_profile", "course") doesn't bind NULL, so an
        account can carry both a legacy and a migrated row for one course."""
        Enrollment.objects.create(
            user=self.legacy_account,
            learner_profile=self.legacy_default,
            course=self.course,
            status=Enrollment.STATUS_ACTIVE,
        )
        url = f"/api/courses/subjects/{self.subject.id}/students/"
        rows = self.client.get(url).json()["students"]

        self.assertEqual(
            [r["id"] for r in rows].count(str(self.legacy_default.id)),
            1,
            "the same student must not appear twice",
        )


# ===================================================================
# RECORDING NOTES — a viewer's own private notes on a recording
# ===================================================================

class RecordingNoteTest(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient
        from .models_recordings import SessionRecording

        self.teacher = User.objects.create_user(
            username="rn_teacher@x.com", email="rn_teacher@x.com", password="x"
        )
        teacher_role, _ = Role.objects.get_or_create(name="TEACHER")
        UserRole.objects.create(
            user=self.teacher, role=teacher_role, is_active=True, is_primary=True
        )

        self.student = User.objects.create_user(
            username="rn_student@x.com", email="rn_student@x.com", password="x"
        )
        self.outsider = User.objects.create_user(
            username="rn_out@x.com", email="rn_out@x.com", password="x"
        )

        self.course = Course.objects.create(title="C10", class_level=10)
        self.subject = Subject.objects.create(course=self.course, name="Physics")
        TeachingAssignment.objects.create(subject=self.subject, teacher=self.teacher, batch=None, is_active=True)
        # A REAL learner: profile + profile-scoped enrolment. The recording
        # guard is profile-and-batch scoped (a sibling's enrolment must not
        # authorise another child, and a batch-scoped recording must not leak
        # across batches), so an account-only enrolment is not a valid fixture
        # any more — and never matched production, where a learner always has
        # an active profile in context.
        from accounts.models import LearnerProfile
        self.student_profile = LearnerProfile.objects.create(
            account=self.student, display_name="Stu", is_default=True,
        )
        Enrollment.objects.create(
            user=self.student, learner_profile=self.student_profile,
            course=self.course, status=Enrollment.STATUS_ACTIVE,
        )

        self.recording = SessionRecording.objects.create(
            subject=self.subject, title="Ch1 recording",
            bunny_video_id="vid123", uploaded_by=self.teacher,
        )
        self.APIClient = APIClient

    def _client(self, user, context="teacher"):
        client = self.APIClient()
        token = {"context": context}
        if context == "learner":
            profile = getattr(self, "student_profile", None)
            if profile is not None and user == self.student:
                token["active_profile"] = str(profile.id)
        client.force_authenticate(user=user, token=token)
        return client

    def test_teacher_can_save_and_read_own_note(self):
        client = self._client(self.teacher)
        r = client.patch(
            f"/api/courses/recordings/{self.recording.id}/notes/",
            {"content": "Class ran long, revisit refraction next time."},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)

        r = client.get(f"/api/courses/recordings/{self.recording.id}/notes/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["content"], "Class ran long, revisit refraction next time.")

    def test_enrolled_student_can_save_own_note(self):
        client = self._client(self.student, context="learner")
        r = client.patch(
            f"/api/courses/recordings/{self.recording.id}/notes/",
            {"content": "Remember: refraction formula."},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)

    def test_notes_are_private_per_user(self):
        self._client(self.teacher).patch(
            f"/api/courses/recordings/{self.recording.id}/notes/",
            {"content": "Teacher's note"}, format="json",
        )
        self._client(self.student, context="learner").patch(
            f"/api/courses/recordings/{self.recording.id}/notes/",
            {"content": "Student's note"}, format="json",
        )
        teacher_view = self._client(self.teacher).get(
            f"/api/courses/recordings/{self.recording.id}/notes/"
        )
        student_view = self._client(self.student, context="learner").get(
            f"/api/courses/recordings/{self.recording.id}/notes/"
        )
        self.assertEqual(teacher_view.data["content"], "Teacher's note")
        self.assertEqual(student_view.data["content"], "Student's note")

    def test_unrelated_user_rejected(self):
        client = self._client(self.outsider, context="learner")
        r = client.get(f"/api/courses/recordings/{self.recording.id}/notes/")
        self.assertEqual(r.status_code, 403)


class SubjectAccessTests(TestCase):
    """Regression cover for a paywall bypass: ``SubjectDetailView`` and
    ``SubjectChaptersView`` used to only check ``IsAuthenticated``, so any
    signed-up account (enrolled or not) could read another course's chapter
    content and roster by guessing/enumerating ``subject_id``. Both now share
    ``_require_subject_access`` with the always-correct ``SubjectDashboardView``.
    """

    def setUp(self):
        from rest_framework.test import APIClient

        self.student = User.objects.create_user(
            username="sa_student@x.com", email="sa_student@x.com", password="x",
        )
        self.outsider = User.objects.create_user(
            username="sa_outsider@x.com", email="sa_outsider@x.com", password="x",
        )
        self.teacher = User.objects.create_user(
            username="sa_teacher@x.com", email="sa_teacher@x.com", password="x",
        )
        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        UserRole.objects.create(
            user=self.teacher, role=teacher_role, is_active=True, is_primary=True,
        )

        self.course = Course.objects.create(title="C10 Science")
        self.subject = Subject.objects.create(course=self.course, name="Physics")
        TeachingAssignment.objects.create(subject=self.subject, teacher=self.teacher, batch=None, is_active=True)
        Chapter.objects.create(
            subject=self.subject, title="Ch1", order=1,
            content_html="<p>paid content</p>",
        )

        self.student_profile = LearnerProfile.objects.create(
            account=self.student, display_name="Student One",
            full_name="Student One", is_default=True,
        )
        Enrollment.objects.create(
            user=self.student, learner_profile=self.student_profile,
            course=self.course, status=Enrollment.STATUS_ACTIVE,
        )

        # Signed up, but enrolled in nothing and not a teacher on this subject.
        self.outsider_profile = LearnerProfile.objects.create(
            account=self.outsider, display_name="Outsider",
            full_name="Outsider", is_default=True,
        )
        self.APIClient = APIClient

    def _client(self, user, profile=None):
        client = self.APIClient()
        token = {"context": "learner" if profile else "teacher"}
        if profile is not None:
            token["active_profile"] = str(profile.id)
        client.force_authenticate(user=user, token=token)
        return client

    def test_unenrolled_signed_up_user_cannot_read_subject_detail(self):
        client = self._client(self.outsider, profile=self.outsider_profile)
        r = client.get(f"/api/courses/subject/{self.subject.id}/")
        self.assertEqual(r.status_code, 403)

    def test_unenrolled_signed_up_user_cannot_read_chapters(self):
        client = self._client(self.outsider, profile=self.outsider_profile)
        r = client.get(f"/api/courses/subjects/{self.subject.id}/chapters/")
        self.assertEqual(r.status_code, 403)

    def test_enrolled_student_can_read_subject_detail(self):
        client = self._client(self.student, profile=self.student_profile)
        r = client.get(f"/api/courses/subject/{self.subject.id}/")
        self.assertEqual(r.status_code, 200)

    def test_enrolled_student_can_read_chapters(self):
        client = self._client(self.student, profile=self.student_profile)
        r = client.get(f"/api/courses/subjects/{self.subject.id}/chapters/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()[0]["title"], "Ch1")

    def test_assigned_teacher_can_read_chapters(self):
        client = self._client(self.teacher)
        r = client.get(f"/api/courses/subjects/{self.subject.id}/chapters/")
        self.assertEqual(r.status_code, 200)


class AcademyTeacherPickerTrackTests(TestCase):
    """Regression cover: the admin subject-teacher picker (and the assign
    write-paths behind it) gated on ``TeacherProfile.is_approved``, which is
    ``bool(approved_tracks())``. The Skill track is AUTO-approved at signup with
    no admin review (accounts/signup_serializer._initial_status_for), so every
    self-registered guest expert had is_approved=True and was offered as — and
    could be assigned as — a school subject teacher, despite academy_status
    being ``locked``. Both now gate on an APPROVED Academy track.
    """

    @classmethod
    def setUpTestData(cls):
        from accounts.models import TeacherProfile

        cls.teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)

        cls.admin = User.objects.create_user(
            username="admin_picker", email="admin_picker@example.com",
            password="pw", is_staff=True,
        )

        cls.course = Course.objects.create(title="Class 10 Science")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")

        A = TeacherProfile.TRACK_APPROVED
        P = TeacherProfile.TRACK_PENDING
        cls.faculty = cls._make("faculty_only", academy=A)
        cls.both = cls._make("both_tracks", academy=A, skill=A)
        cls.skill_only = cls._make("skill_only", skill=A)      # self-signup expert
        cls.pending = cls._make("pending_faculty", academy=P)  # awaiting review

    @classmethod
    def _make(cls, name, academy=None, skill=None):
        from accounts.models import TeacherProfile

        user = User.objects.create_user(
            username=name, email=f"{name}@example.com", password="pw",
        )
        tp = TeacherProfile.objects.create(user=user)
        if academy:
            tp.set_track_status(TeacherProfile.TRACK_ACADEMY, academy)
        if skill:
            tp.set_track_status(TeacherProfile.TRACK_SKILL, skill)
        tp.sync_type_from_tracks()
        tp.save()
        UserRole.objects.create(
            user=user, role=cls.teacher_role, is_active=bool(tp.approved_tracks()),
        )
        return user

    def _admin_client(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.admin, token={"context": "admin"})
        return client

    def test_picker_offers_academy_faculty_and_excludes_skill_only_experts(self):
        r = self._admin_client().get("/api/courses/admin/teachers/")
        self.assertEqual(r.status_code, 200, r.content)
        emails = {row["email"] for row in r.json()["data"]}

        # Reviewed Academy faculty — and a teacher holding BOTH tracks, who
        # really is faculty — must still be offered.
        self.assertIn("faculty_only@example.com", emails)
        self.assertIn("both_tracks@example.com", emails)
        # The auto-approved guest expert and the un-reviewed applicant must not.
        self.assertNotIn("skill_only@example.com", emails)
        self.assertNotIn("pending_faculty@example.com", emails)

    def test_picker_reports_a_real_total_not_a_silent_truncation(self):
        r = self._admin_client().get("/api/courses/admin/teachers/")
        body = r.json()
        self.assertEqual(body["count"], 2)
        self.assertFalse(body["has_more"])

    def test_picker_exposes_tracks_so_the_admin_can_tell_them_apart(self):
        r = self._admin_client().get("/api/courses/admin/teachers/")
        rows = {row["email"]: row for row in r.json()["data"]}
        self.assertEqual(rows["faculty_only@example.com"]["tracks"], ["academy"])
        self.assertEqual(rows["both_tracks@example.com"]["tracks"], ["academy", "skill"])

    def test_search_is_applied_server_side(self):
        r = self._admin_client().get("/api/courses/admin/teachers/?q=both_tracks")
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["data"][0]["email"], "both_tracks@example.com")

    def test_skill_only_expert_cannot_be_assigned_to_a_subject(self):
        """The picker no longer offers them, but the write-path must refuse too —
        filtering a dropdown is not authorization."""
        r = self._admin_client().post(
            f"/api/courses/admin/subjects/{self.subject.id}/teachers/",
            {"teacher_id": str(self.skill_only.id)}, format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(
            TeachingAssignment.objects.filter(
                subject=self.subject, teacher=self.skill_only, batch__isnull=True, is_active=True).exists()
        )

    def test_academy_faculty_can_still_be_assigned_to_a_subject(self):
        r = self._admin_client().post(
            f"/api/courses/admin/subjects/{self.subject.id}/teachers/",
            {"teacher_id": str(self.faculty.id)}, format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(
            TeachingAssignment.objects.filter(
                subject=self.subject, teacher=self.faculty, batch__isnull=True, is_active=True).exists()
        )


class CourseStaffingGridTests(AcademyTeacherPickerTrackTests):
    """The whole-course staffing grid + bulk assign, which replace rendering the
    subjects table via one request per subject."""

    def test_grid_returns_every_subject_in_one_call(self):
        maths = Subject.objects.create(course=self.course, name="Maths")
        TeachingAssignment.objects.create(subject=self.subject, teacher=self.faculty, batch=None, is_active=True)

        r = self._admin_client().get(
            f"/api/courses/admin/courses/{self.course.id}/staffing/")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()

        names = {s["name"]: s for s in body["subjects"]}
        self.assertEqual(set(names), {"Physics", "Maths"})
        self.assertEqual(
            [t["email"] for t in names["Physics"]["teachers"]],
            ["faculty_only@example.com"],
        )
        self.assertEqual(names["Maths"]["teachers"], [])
        # Drives the "N subjects still need a teacher" hint.
        self.assertEqual(body["unstaffed_count"], 1)
        self.assertEqual(maths.name, "Maths")  # created above, still present

    def test_bulk_assign_staffs_many_subjects_at_once(self):
        maths = Subject.objects.create(course=self.course, name="Maths")
        chem = Subject.objects.create(course=self.course, name="Chemistry")

        r = self._admin_client().post(
            f"/api/courses/admin/courses/{self.course.id}/staffing/bulk-assign/",
            {"teacher_id": str(self.faculty.id),
             "subject_ids": [str(maths.id), str(chem.id)]},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["assigned"], 2)
        self.assertEqual(
            TeachingAssignment.objects.filter(teacher=self.faculty, batch__isnull=True, is_active=True).count(), 2)

    def test_bulk_assign_is_idempotent_rather_than_failing_the_whole_call(self):
        maths = Subject.objects.create(course=self.course, name="Maths")
        TeachingAssignment.objects.create(subject=maths, teacher=self.faculty, batch=None, is_active=True)
        chem = Subject.objects.create(course=self.course, name="Chemistry")

        r = self._admin_client().post(
            f"/api/courses/admin/courses/{self.course.id}/staffing/bulk-assign/",
            {"teacher_id": str(self.faculty.id),
             "subject_ids": [str(maths.id), str(chem.id)]},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["assigned"], 1)                      # chem only
        self.assertEqual(body["skipped_already_assigned"], [str(maths.id)])

    def test_bulk_assign_refuses_a_subject_from_another_course(self):
        other = Course.objects.create(title="Class 9 Science")
        foreign = Subject.objects.create(course=other, name="Biology")

        r = self._admin_client().post(
            f"/api/courses/admin/courses/{self.course.id}/staffing/bulk-assign/",
            {"teacher_id": str(self.faculty.id),
             "subject_ids": [str(foreign.id)]},
            format="json",
        )
        self.assertEqual(r.json()["skipped_not_in_course"], [str(foreign.id)])
        self.assertFalse(
            TeachingAssignment.objects.filter(subject=foreign, batch__isnull=True, is_active=True).exists())

    def test_bulk_assign_refuses_a_skill_only_expert(self):
        maths = Subject.objects.create(course=self.course, name="Maths")
        r = self._admin_client().post(
            f"/api/courses/admin/courses/{self.course.id}/staffing/bulk-assign/",
            {"teacher_id": str(self.skill_only.id),
             "subject_ids": [str(maths.id)]},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(
            TeachingAssignment.objects.filter(teacher=self.skill_only, batch__isnull=True, is_active=True).exists())


class TeachingAssignmentRevocationTests(TestCase):
    """Ending a TeachingAssignment must actually revoke the teacher.

    Regression cover from when TeachingAssignment DELETE used to leave a
    mirrored legacy SubjectTeacher row in place (services.teaches_subject()
    granted access on either model having a row) — "remove this teacher" left
    them with full subject access (quizzes, materials, recordings,
    livestream). SubjectTeacher has since been retired entirely, so
    TeachingAssignment is now the only thing that can grant access; kept as a
    regression test since the same class of bug (a second, forgotten grant
    path) is easy to reintroduce.
    """

    @classmethod
    def setUpTestData(cls):
        from accounts.models import TeacherProfile

        cls.teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        cls.admin = User.objects.create_user(
            username="admin_rev", email="admin_rev@example.com",
            password="pw", is_staff=True,
        )
        cls.teacher = User.objects.create_user(
            username="rev_teacher", email="rev_teacher@example.com", password="pw",
        )
        tp = TeacherProfile.objects.create(user=cls.teacher)
        tp.set_track_status(TeacherProfile.TRACK_ACADEMY, TeacherProfile.TRACK_APPROVED)
        tp.sync_type_from_tracks()
        tp.save()
        UserRole.objects.create(user=cls.teacher, role=cls.teacher_role, is_active=True)

        cls.course = Course.objects.create(title="Class 10 Science")
        cls.subject = Subject.objects.create(course=cls.course, name="Physics")

    def _admin_client(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.admin, token={"context": "admin"})
        return client

    def _assign_to_batch(self, batch):
        client = self._admin_client()
        r = client.post(
            f"/api/courses/admin/batches/{batch.id}/teaching-assignments/",
            {"subject_id": str(self.subject.id),
             "teacher_id": str(self.teacher.id), "role": "PRIMARY"},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["assignment_id"]

    def test_ending_the_last_assignment_revokes_subject_access(self):
        from .models import Batch
        from .services import teaches_subject

        batch = Batch.objects.create(course=self.course, name="2026 A", code="A")
        assignment_id = self._assign_to_batch(batch)
        self.assertTrue(teaches_subject(self.teacher, self.subject))

        r = self._admin_client().delete(
            f"/api/courses/admin/teaching-assignments/{assignment_id}/")
        self.assertEqual(r.status_code, 204, r.content)

        self.assertFalse(
            teaches_subject(self.teacher, self.subject),
            "ending the only assignment must actually revoke access",
        )

    def test_ending_one_of_two_assignments_keeps_access(self):
        from .models import Batch
        from .services import teaches_subject

        a = Batch.objects.create(course=self.course, name="2026 A", code="A")
        b = Batch.objects.create(course=self.course, name="2026 B", code="B")
        first = self._assign_to_batch(a)
        self._assign_to_batch(b)

        self._admin_client().delete(
            f"/api/courses/admin/teaching-assignments/{first}/")

        self.assertTrue(
            teaches_subject(self.teacher, self.subject),
            "the teacher still teaches this subject in the other batch",
        )


class RecordingAccessControlTest(TestCase):
    """Per-id recording endpoints must not be a side door around the list.

    RecordingDetailView and CheckVideoStatusView fetched by pk with NO
    entitlement check at all, while SubjectRecordingsView right beside them
    did teacher/batch scoping properly. The serializer emits bunny_video_id,
    and playback URLs in this codebase are built unsigned — so a leaked id is
    a directly playable class recording, including unpublished drafts.
    """

    @classmethod
    def setUpTestData(cls):
        from accounts.models import LearnerProfile
        from courses.models_recordings import SessionRecording

        Role.objects.get_or_create(name="STUDENT")
        Role.objects.get_or_create(name="TEACHER")

        cls.teacher = User.objects.create_user(
            username="rec_t", email="rec_t@test.com", password="x")
        # The guard's teacher branch needs BOTH teacher context and the role.
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True)
        cls.course = Course.objects.create(title="Class 9")
        cls.subject = Subject.objects.create(course=cls.course, name="Chem")
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, batch=None, is_active=True)

        cls.morning = Batch.objects.create(course=cls.course, name="Morning", code="RM")
        cls.evening = Batch.objects.create(course=cls.course, name="Evening", code="RE")

        def learner(username, batch):
            u = User.objects.create_user(
                username=username, email=f"{username}@t.com", password="x")
            p = LearnerProfile.objects.create(
                account=u, display_name=username, is_default=True)
            Enrollment.objects.create(
                user=u, learner_profile=p, course=cls.course,
                batch=batch, status=Enrollment.STATUS_ACTIVE)
            return u, p

        cls.mine_user, cls.mine_profile = learner("rec_morning", cls.morning)
        cls.other_user, cls.other_profile = learner("rec_evening", cls.evening)

        # Enrolled in nothing at all.
        cls.outsider = User.objects.create_user(
            username="rec_out", email="rec_out@t.com", password="x")
        LearnerProfile.objects.create(
            account=cls.outsider, display_name="Out", is_default=True)

        cls.shared = SessionRecording.objects.create(
            subject=cls.subject, title="Course-wide", batch=None,
            bunny_video_id="vid-shared", uploaded_by=cls.teacher)
        cls.morning_only = SessionRecording.objects.create(
            subject=cls.subject, title="Morning only", batch=cls.morning,
            bunny_video_id="vid-morning", uploaded_by=cls.teacher)

    def _client(self, user, profile=None):
        c = APIClient()
        token = {"context": "learner"}
        if profile is not None:
            token["active_profile"] = str(profile.id)
        c.force_authenticate(user=user, token=token)
        return c

    def test_outsider_cannot_read_a_recording_by_id(self):
        c = self._client(self.outsider,
                         self.outsider.learner_profiles.first())
        r = c.get(f"/api/courses/recordings/{self.shared.id}/")
        self.assertEqual(r.status_code, 403, r.content)
        self.assertNotIn("bunny_video_id", str(r.content))

    def test_outsider_cannot_reach_the_status_endpoint_either(self):
        # Second door to the same payload — and it burns a Bunny API call.
        c = self._client(self.outsider,
                         self.outsider.learner_profiles.first())
        r = c.get(f"/api/courses/recordings/{self.shared.id}/status/")
        self.assertEqual(r.status_code, 403, r.content)

    def test_wrong_batch_learner_cannot_read_a_batch_scoped_recording(self):
        c = self._client(self.other_user, self.other_profile)
        r = c.get(f"/api/courses/recordings/{self.morning_only.id}/")
        self.assertEqual(r.status_code, 403, r.content)

    def test_the_right_learner_still_gets_their_own_recordings(self):
        c = self._client(self.mine_user, self.mine_profile)
        for rec in (self.shared, self.morning_only):
            r = c.get(f"/api/courses/recordings/{rec.id}/")
            self.assertEqual(r.status_code, 200, r.content)
        # Course-wide stays visible to the other batch too.
        c2 = self._client(self.other_user, self.other_profile)
        self.assertEqual(
            c2.get(f"/api/courses/recordings/{self.shared.id}/").status_code, 200)

    def test_teacher_of_the_subject_sees_every_batch(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        r = c.get(f"/api/courses/recordings/{self.morning_only.id}/")
        self.assertEqual(r.status_code, 200, r.content)


class SignedUploadUrlOwnershipTest(TestCase):
    """SignedUploadUrlView previously signed a valid Bunny TUS upload ticket
    for ANY client-supplied video_id with no ownership check at all — any
    teacher-context account could get a ticket to overwrite another
    teacher's recording, since bunny_video_id is serialized back to
    teachers elsewhere in this app."""

    @classmethod
    def setUpTestData(cls):
        from courses.models_recordings import SessionRecording, PendingVideoUpload
        cls.PendingVideoUpload = PendingVideoUpload

        Role.objects.get_or_create(name="TEACHER")
        cls.teacher_a = User.objects.create_user(
            username="up_a", email="up_a@test.com", password="x")
        cls.teacher_b = User.objects.create_user(
            username="up_b", email="up_b@test.com", password="x")
        for t in (cls.teacher_a, cls.teacher_b):
            UserRole.objects.create(
                user=t, role=Role.objects.get(name="TEACHER"), is_active=True, is_primary=True)

        cls.course = Course.objects.create(title="Class 9")
        cls.subject_a = Subject.objects.create(course=cls.course, name="Chem")
        TeachingAssignment.objects.create(
            subject=cls.subject_a, teacher=cls.teacher_a, batch=None, is_active=True)

        cls.existing_recording = SessionRecording.objects.create(
            subject=cls.subject_a, title="Existing", batch=None,
            bunny_video_id="vid-existing", uploaded_by=cls.teacher_a,
        )

    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user, token={"context": "teacher"})
        return c

    def test_cannot_sign_an_unowned_arbitrary_video_id(self):
        r = self._client(self.teacher_b).post(
            "/api/courses/recordings/signed-upload-url/", {"video_id": "vid-not-mine"}, format="json")
        self.assertEqual(r.status_code, 403, r.content)

    def test_cannot_sign_another_teachers_existing_recording(self):
        r = self._client(self.teacher_b).post(
            "/api/courses/recordings/signed-upload-url/", {"video_id": "vid-existing"}, format="json")
        self.assertEqual(r.status_code, 403, r.content)

    def test_owner_can_sign_their_own_existing_recording(self):
        r = self._client(self.teacher_a).post(
            "/api/courses/recordings/signed-upload-url/", {"video_id": "vid-existing"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_creator_can_sign_their_own_pending_slot(self):
        self.PendingVideoUpload.objects.create(video_id="vid-fresh", created_by=self.teacher_b)
        r = self._client(self.teacher_b).post(
            "/api/courses/recordings/signed-upload-url/", {"video_id": "vid-fresh"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_other_teacher_cannot_sign_someone_elses_pending_slot(self):
        self.PendingVideoUpload.objects.create(video_id="vid-fresh2", created_by=self.teacher_a)
        r = self._client(self.teacher_b).post(
            "/api/courses/recordings/signed-upload-url/", {"video_id": "vid-fresh2"}, format="json")
        self.assertEqual(r.status_code, 403, r.content)

    def test_create_video_slot_records_ownership(self):
        # Confirms CreateVideoSlotView actually writes the row the
        # ownership check above depends on — belt and suspenders against a
        # future change to that view silently dropping it again.
        from unittest.mock import patch, Mock
        with patch("courses.views_recordings.requests.post") as mock_post:
            mock_post.return_value = Mock(status_code=201, json=lambda: {"guid": "vid-new"})
            r = self._client(self.teacher_a).post("/api/courses/recordings/create-video/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(
            self.PendingVideoUpload.objects.filter(video_id="vid-new", created_by=self.teacher_a).exists()
        )


class CourseCatalogVisibilityTest(TestCase):
    """Browse Courses (GET /courses/catalog/) — which courses are listed, and
    whose enrolment marks one as owned.

    The catalog required `batches__is_active=True` for every PUBLISHED course.
    Batches arrived with the catalog-vs-delivery refactor but were never
    populated, so on production that hid all 13 real classes and left only the
    non-purchasable COMING_SOON placeholders — the Browse screen showed a
    learner nothing they could actually enrol in.
    """

    @classmethod
    def setUpTestData(cls):
        from accounts.models import LearnerProfile
        Role.objects.get_or_create(name="STUDENT")

        cls.board = Board.objects.create(name="CBSE", board_type=Board.TYPE_CENTRAL)

        # No batches at all → course-wide, must be listed.
        cls.no_batch = Course.objects.create(
            title="Class 9", board=cls.board, status=Course.STATUS_PUBLISHED)
        # Has a live cohort → listed.
        cls.active_batch = Course.objects.create(
            title="Class 10", board=cls.board, status=Course.STATUS_PUBLISHED)
        Batch.objects.create(course=cls.active_batch, name="Morning", code="M1", is_active=True)
        # Had cohorts, none running → deliberately withheld.
        cls.stale_batch = Course.objects.create(
            title="Class 11", board=cls.board, status=Course.STATUS_PUBLISHED)
        Batch.objects.create(course=cls.stale_batch, name="Finished", code="F1", is_active=False)
        # Not ready → never listed.
        cls.draft = Course.objects.create(
            title="Class 12 draft", board=cls.board, status=Course.STATUS_DRAFT)
        # Shown but not purchasable.
        cls.soon = Course.objects.create(
            title="NEET", board=cls.board, status=Course.STATUS_COMING_SOON)

        cls.parent = User.objects.create_user(
            username="cat_parent", email="cat_parent@t.com", password="x")
        cls.child_a = LearnerProfile.objects.create(
            account=cls.parent, display_name="A", is_default=True)
        cls.child_b = LearnerProfile.objects.create(
            account=cls.parent, display_name="B", is_default=False)

    def _client(self, profile):
        c = APIClient()
        c.force_authenticate(user=self.parent,
                             token={"context": "learner", "active_profile": str(profile.id)})
        return c

    def _titles(self, profile):
        res = self._client(profile).get("/api/courses/catalog/")
        self.assertEqual(res.status_code, 200, res.content)
        return {row["title"]: row for row in res.data}

    def test_published_course_with_no_batches_is_listed(self):
        rows = self._titles(self.child_a)
        self.assertIn("Class 9", rows, "a published course with no batch must not be hidden")
        self.assertIn("Class 10", rows)

    def test_course_whose_only_batch_ended_is_withheld(self):
        # Distinct from "no batches": this one genuinely has no running cohort.
        self.assertNotIn("Class 11", self._titles(self.child_a))

    def test_draft_is_never_listed_and_coming_soon_is_not_purchasable(self):
        rows = self._titles(self.child_a)
        self.assertNotIn("Class 12 draft", rows)
        self.assertIs(rows["NEET"]["is_coming_soon"], True)

    def test_one_childs_enrolment_does_not_mark_the_course_owned_for_a_sibling(self):
        Enrollment.objects.create(
            user=self.parent, learner_profile=self.child_a,
            course=self.no_batch, status=Enrollment.STATUS_ACTIVE)
        self.assertIs(self._titles(self.child_a)["Class 9"]["is_enrolled"], True)
        # Was True for B as well — keyed on the account, so a sibling's course
        # showed as Enrolled and B could not enrol in it at all.
        self.assertIs(self._titles(self.child_b)["Class 9"]["is_enrolled"], False)

    def test_teacher_preview_shows_a_batch_scoped_assignment(self):
        # Previewing required batch__isnull=True, so once staffing moved to
        # per-batch rows no teacher rendered on any card.
        teacher = User.objects.create_user(
            username="cat_teacher", email="cat_teacher@t.com", password="x")
        subject = Subject.objects.create(course=self.active_batch, name="Maths")
        batch = self.active_batch.batches.first()
        TeachingAssignment.objects.create(
            subject=subject, teacher=teacher, batch=batch, is_active=True)
        self.assertEqual(
            self._titles(self.child_a)["Class 10"]["lead_teacher"], teacher.username)

    def test_deleted_teacher_does_not_500_the_whole_catalog(self):
        """A hard-deleted teacher used to take Browse Courses down platform-wide.

        TeachingAssignment.teacher is SET_NULL, and the admin soft-end path
        only flips is_active — so deleting a teacher ACCOUNT leaves an active
        row with teacher_id NULL. The catalog then called
        `link.teacher.default_learner_profile()` on None, DRF let the
        AttributeError through, and every learner got Django's bare
        "Server Error (500)" HTML page instead of the shop.
        """
        teacher = User.objects.create_user(
            username="doomed", email="doomed@t.com", password="x")
        subject = Subject.objects.create(course=self.no_batch, name="Physics")
        TeachingAssignment.objects.create(
            subject=subject, teacher=teacher, is_active=True)
        teacher.delete()

        self.assertTrue(
            TeachingAssignment.objects.filter(
                subject=subject, teacher__isnull=True, is_active=True).exists(),
            "precondition: deletion must leave an ACTIVE row with a NULL teacher",
        )

        rows = self._titles(self.child_a)          # asserts 200, not 500
        self.assertIsNone(rows["Class 9"]["lead_teacher"])

    def test_a_live_substitute_is_named_even_if_the_lead_was_deleted(self):
        """Skipping the NULL row inside the loop would not have been enough.

        The loop keeps the FIRST row per course and `continue`s past the rest,
        with PRIMARY ordered first — so a deleted lead would have shadowed a
        perfectly good substitute and the card would show no teacher at all.
        Excluding NULL rows in the QUERY is what makes the substitute surface.
        """
        lead = User.objects.create_user(
            username="lead", email="lead@t.com", password="x")
        sub = User.objects.create_user(
            username="sub", email="sub@t.com", password="x")
        subject = Subject.objects.create(course=self.no_batch, name="Chemistry")
        TeachingAssignment.objects.create(
            subject=subject, teacher=lead, is_active=True,
            role=TeachingAssignment.ROLE_PRIMARY, order=1)
        TeachingAssignment.objects.create(
            subject=subject, teacher=sub, is_active=True,
            role=TeachingAssignment.ROLE_ASSISTANT, order=2)
        lead.delete()

        self.assertEqual(self._titles(self.child_a)["Class 9"]["lead_teacher"], "sub")
