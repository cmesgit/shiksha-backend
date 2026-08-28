"""Who may switch automatic class recording on for a course.

Egress is billed per minute, so `Course.auto_record_enabled` is a spending
control, not a display preference. It is read-only on CourseSerializer and
written only by AdminCourseDetailView, because UpdateCourseView forwards its
whole payload to the same serializer with no allowlist and is merely
IsTeacherContext — a writable field there would let any teacher assigned to a
subject start billing egress for the entire course.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Role, UserRole
from courses.models import Board, Course, Subject, TeachingAssignment

User = get_user_model()


class AutoRecordWritePermissionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        cls.board = Board.objects.create(name="ARBoard", board_type=Board.TYPE_CENTRAL)
        cls.course = Course.objects.create(
            board=cls.board, title="AR10", class_level=10)
        cls.subject = Subject.objects.create(course=cls.course, name="Maths")

        cls.teacher = User.objects.create_user(
            username="ar_t@x.com", email="ar_t@x.com", password="pw")
        UserRole.objects.create(user=cls.teacher, role=teacher_role)
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, batch=None, is_active=True)

        cls.admin = User.objects.create_user(
            username="ar_a@x.com", email="ar_a@x.com", password="pw",
            is_staff=True, is_superuser=True)

    def admin_client(self):
        c = APIClient()
        c.force_authenticate(user=self.admin)
        return c

    def teacher_client(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher)
        c.cookies["access_token"] = "x"
        return c

    def admin_url(self):
        return f"/api/courses/admin/courses/{self.course.id}/"

    def test_defaults_to_null_meaning_follow_the_global_setting(self):
        self.assertIsNone(self.course.auto_record_enabled)

    def test_admin_can_turn_it_on(self):
        res = self.admin_client().patch(
            self.admin_url(), {"auto_record_enabled": True}, format="json")
        self.assertEqual(res.status_code, 200)
        self.course.refresh_from_db()
        self.assertTrue(self.course.auto_record_enabled)

    def test_admin_can_turn_it_off_explicitly(self):
        self.course.auto_record_enabled = True
        self.course.save(update_fields=["auto_record_enabled"])
        self.admin_client().patch(
            self.admin_url(), {"auto_record_enabled": False}, format="json")
        self.course.refresh_from_db()
        self.assertIs(self.course.auto_record_enabled, False)

    def test_admin_can_reset_it_to_follow_the_global_default(self):
        """null is a real, distinct third state — not the same as False."""
        self.course.auto_record_enabled = False
        self.course.save(update_fields=["auto_record_enabled"])
        self.admin_client().patch(
            self.admin_url(), {"auto_record_enabled": None}, format="json")
        self.course.refresh_from_db()
        self.assertIsNone(self.course.auto_record_enabled)

    def test_omitting_the_key_leaves_it_untouched(self):
        self.course.auto_record_enabled = True
        self.course.save(update_fields=["auto_record_enabled"])
        self.admin_client().patch(
            self.admin_url(), {"title": "Renamed"}, format="json")
        self.course.refresh_from_db()
        self.assertTrue(self.course.auto_record_enabled)
        self.assertEqual(self.course.title, "Renamed")

    def test_string_forms_are_accepted_for_multipart_submissions(self):
        """The admin course form posts multipart when a thumbnail is attached,
        which turns every value into a string."""
        for raw, expected in (("true", True), ("false", False),
                              ("1", True), ("0", False)):
            with self.subTest(raw=raw):
                self.admin_client().patch(
                    self.admin_url(), {"auto_record_enabled": raw},
                    format="json")
                self.course.refresh_from_db()
                self.assertIs(self.course.auto_record_enabled, expected)

    def test_create_honours_the_flag_instead_of_dropping_it(self):
        """The admin course form sends one payload shape for create and edit.
        A key accepted on PATCH but silently ignored on POST is the failure
        mode that made a full_name PATCH vanish with no error."""
        res = self.admin_client().post(
            "/api/courses/admin/courses/",
            {"title": "Recorded course", "board_id": str(self.board.id),
             "auto_record_enabled": True},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201), res.data)
        created = Course.objects.get(title="Recorded course")
        self.assertTrue(created.auto_record_enabled)

    def test_create_without_the_flag_inherits_the_global_default(self):
        res = self.admin_client().post(
            "/api/courses/admin/courses/",
            {"title": "Plain course", "board_id": str(self.board.id)},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201), res.data)
        self.assertIsNone(
            Course.objects.get(title="Plain course").auto_record_enabled)

    def test_teacher_cannot_switch_recording_on(self):
        """The reason the field is read-only on the serializer: this endpoint
        forwards the whole payload and is only IsTeacherContext."""
        res = self.teacher_client().patch(
            f"/api/courses/{self.course.id}/update/",
            {"auto_record_enabled": True}, format="json")
        self.course.refresh_from_db()
        self.assertIsNone(
            self.course.auto_record_enabled,
            f"teacher write leaked through (status {res.status_code})",
        )

    def test_the_flag_is_visible_to_read_it(self):
        """Read-only does not mean hidden — the admin UI has to show the
        current state, and a teacher seeing whether their class is recorded is
        reasonable."""
        self.course.auto_record_enabled = True
        self.course.save(update_fields=["auto_record_enabled"])
        res = self.admin_client().get(self.admin_url())
        self.assertTrue(res.data["auto_record_enabled"])
