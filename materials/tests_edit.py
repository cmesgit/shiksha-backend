"""StudyMaterial gained a metadata edit path; it was create/read/delete only.

Two things this must not become:

  * a second, unvalidated write path to MaterialFile — attachments stay behind
    the validated upload flow, and a PATCH carrying files must not quietly
    attach them;
  * a way to move a material onto a different subject. `subject` is the
    authorization anchor, and the editor check runs against the material's
    CURRENT subject — so accepting a new one would let a teacher relocate
    content onto a subject they are staffed on, having passed the gate on the
    subject they were not.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import LearnerProfile, Role, User, UserRole
from courses.models import Batch, Chapter, Course, Subject, TeachingAssignment
from materials.models import StudyMaterial


class MaterialEditTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")
        cls.student_role = Role.objects.create(name="STUDENT")

        cls.uploader = User.objects.create_user(
            username="up", email="up@test.com", password="x")
        cls.coteacher = User.objects.create_user(
            username="co", email="co@test.com", password="x")
        cls.outsider = User.objects.create_user(
            username="out", email="out@test.com", password="x")
        for u in (cls.uploader, cls.coteacher, cls.outsider):
            UserRole.objects.create(
                user=u, role=cls.teacher_role, is_active=True, is_primary=True)

        cls.admin = User.objects.create_user(
            username="adm", email="adm@test.com", password="x", is_staff=True)

        cls.kid_account = User.objects.create_user(
            username="kid", email="kid@test.com", password="x")
        UserRole.objects.create(user=cls.kid_account, role=cls.student_role,
                                is_active=True, is_primary=True)
        cls.kid = LearnerProfile.objects.create(
            account=cls.kid_account, display_name="Kid", is_default=True)

        cls.course = Course.objects.create(title="Physics")
        cls.subject = Subject.objects.create(
            course=cls.course, name="Mechanics")
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Kinematics")
        cls.other_chapter = Chapter.objects.create(
            subject=cls.subject, title="Dynamics")
        cls.batch = Batch.objects.create(
            course=cls.course, name="Batch A", code="A1", year=2026)

        # A different course entirely — the cross-course targets below.
        cls.other_course = Course.objects.create(title="History")
        cls.other_subject = Subject.objects.create(
            course=cls.other_course, name="Modern")
        cls.foreign_chapter = Chapter.objects.create(
            subject=cls.other_subject, title="Empire")
        cls.foreign_batch = Batch.objects.create(
            course=cls.other_course, name="Hist A", code="H1", year=2026)

        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.uploader, is_active=True,
            role=TeachingAssignment.ROLE_PRIMARY)
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.coteacher, is_active=True,
            role=TeachingAssignment.ROLE_ASSISTANT)

    def setUp(self):
        self.material = StudyMaterial.objects.create(
            subject=self.subject, chapter=self.chapter,
            title="Notes", description="old", uploaded_by=self.uploader)

    def url(self):
        return f"/api/materials/materials/{self.material.id}/"

    def as_(self, user, context="teacher"):
        c = APIClient()
        token = {"context": context}
        if context == "learner":
            token["active_profile"] = str(self.kid.id)
        c.force_authenticate(user=user, token=token)
        return c

    # ------------------------------------------------------------------
    # who may edit — the SAME rule delete uses
    # ------------------------------------------------------------------

    def test_uploader_can_edit(self):
        res = self.as_(self.uploader).patch(
            self.url(), {"title": "Better notes"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.material.refresh_from_db()
        self.assertEqual(self.material.title, "Better notes")

    def test_co_teacher_can_edit(self):
        """The list this is reached from returns colleagues' materials. An
        editor rule narrower than the list rule renders buttons that only
        403 — the exact bug delete already had and had to be widened to fix."""
        res = self.as_(self.coteacher).patch(
            self.url(), {"title": "Co edit"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)

    def test_admin_can_edit(self):
        res = self.as_(self.admin, context="account").patch(
            self.url(), {"title": "Admin edit"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)

    def test_unstaffed_teacher_cannot_edit(self):
        res = self.as_(self.outsider).patch(
            self.url(), {"title": "Nope"}, format="json")
        self.assertEqual(res.status_code, 403)
        self.material.refresh_from_db()
        self.assertEqual(self.material.title, "Notes")

    def test_learner_cannot_edit(self):
        res = self.as_(self.kid_account, context="learner").patch(
            self.url(), {"title": "Nope"}, format="json")
        self.assertEqual(res.status_code, 403)
        self.material.refresh_from_db()
        self.assertEqual(self.material.title, "Notes")

    def test_anonymous_cannot_edit(self):
        res = APIClient().patch(
            self.url(), {"title": "Nope"}, format="json")
        self.assertIn(res.status_code, (401, 403))

    # ------------------------------------------------------------------
    # what may be edited
    # ------------------------------------------------------------------

    def test_partial_update_leaves_other_fields_alone(self):
        res = self.as_(self.uploader).patch(
            self.url(), {"description": "new"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.material.refresh_from_db()
        self.assertEqual(self.material.description, "new")
        self.assertEqual(self.material.title, "Notes")

    def test_chapter_can_be_moved_within_the_subject(self):
        res = self.as_(self.uploader).patch(
            self.url(), {"chapter_id": str(self.other_chapter.id)},
            format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.material.refresh_from_db()
        self.assertEqual(self.material.chapter_id, self.other_chapter.id)

    def test_chapter_can_be_cleared(self):
        res = self.as_(self.uploader).patch(
            self.url(), {"chapter_id": None}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.material.refresh_from_db()
        self.assertIsNone(self.material.chapter_id)

    def test_batch_can_be_set_and_cleared(self):
        c = self.as_(self.uploader)
        self.assertEqual(
            c.patch(self.url(), {"batch_id": str(self.batch.id)},
                    format="json").status_code, 200)
        self.material.refresh_from_db()
        self.assertEqual(self.material.batch_id, self.batch.id)

        self.assertEqual(
            c.patch(self.url(), {"batch_id": None},
                    format="json").status_code, 200)
        self.material.refresh_from_db()
        self.assertIsNone(self.material.batch_id)

    def test_updated_at_moves_on_edit(self):
        before = self.material.updated_at
        self.as_(self.uploader).patch(
            self.url(), {"title": "Touched"}, format="json")
        self.material.refresh_from_db()
        self.assertGreater(self.material.updated_at, before)

    # ------------------------------------------------------------------
    # the triangle guard
    # ------------------------------------------------------------------

    def test_editing_the_chapter_keeps_the_chapter_tags_in_step(self):
        """The scalar `chapter` FK must equal primary_chapter() of the
        ContentChapterTag rows — courses/chapter_tags.py calls that the additive
        invariant, and the upload path maintains it with set_tags().

        Writing the FK alone left the two disagreeing, and the PATCH response
        proved it: chapter_title came from the FK while chapter_tags came from
        the stale rows, so one edit returned a material filed under two
        different chapters at once.
        """
        from courses.chapter_tags import set_tags, tags_for

        set_tags(self.material, [(self.chapter, "", 0)])
        self.assertEqual(
            [t.chapter_id for t in tags_for(self.material)], [self.chapter.id]
        )

        res = self.as_(self.uploader).patch(
            self.url(), {"chapter_id": str(self.other_chapter.id)},
            format="json")
        self.assertEqual(res.status_code, 200, res.content)

        self.material.refresh_from_db()
        self.assertEqual(self.material.chapter_id, self.other_chapter.id)
        self.assertEqual(
            [t.chapter_id for t in tags_for(self.material)],
            [self.other_chapter.id],
        )
        # The response the client patches its row from must agree with itself.
        body = res.json()
        self.assertEqual(body["chapter_title"], self.other_chapter.title)
        self.assertEqual(
            [t["chapter_id"] for t in body["chapter_tags"]],
            [str(self.other_chapter.id)],
        )

    def test_clearing_the_chapter_clears_the_tags(self):
        from courses.chapter_tags import set_tags, tags_for

        set_tags(self.material, [(self.chapter, "", 0)])
        res = self.as_(self.uploader).patch(
            self.url(), {"chapter_id": None}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.material.refresh_from_db()
        self.assertIsNone(self.material.chapter_id)
        self.assertEqual(list(tags_for(self.material)), [])

    def test_setting_a_chapter_clears_no_specific_chapter(self):
        """validate_tag_payload refuses "no specific chapter" alongside real
        chapters on create; the edit path must not be able to manufacture that
        contradiction either."""
        StudyMaterial.objects.filter(id=self.material.id).update(
            no_specific_chapter=True, chapter=None)
        res = self.as_(self.uploader).patch(
            self.url(), {"chapter_id": str(self.chapter.id)}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.material.refresh_from_db()
        self.assertEqual(self.material.chapter_id, self.chapter.id)
        self.assertFalse(self.material.no_specific_chapter)

    def test_title_only_edit_does_not_churn_the_tags(self):
        """set_tags DELETEs and re-creates, so running it on every save would
        discard multi-chapter and free-text placement the form never showed."""
        from courses.chapter_tags import set_tags, tags_for

        set_tags(self.material, [(self.chapter, "", 0), (None, "Revision", 1)])
        before = [(t.chapter_id, t.label) for t in tags_for(self.material)]

        self.as_(self.uploader).patch(
            self.url(), {"title": "Renamed only"}, format="json")

        after = [(t.chapter_id, t.label) for t in tags_for(self.material)]
        self.assertEqual(before, after)

    def test_chapter_from_another_subject_is_refused(self):
        res = self.as_(self.uploader).patch(
            self.url(), {"chapter_id": str(self.foreign_chapter.id)},
            format="json")
        self.assertEqual(res.status_code, 400)
        self.material.refresh_from_db()
        self.assertEqual(self.material.chapter_id, self.chapter.id)

    def test_batch_from_another_course_is_refused(self):
        """Without this a Physics handout files under a History batch — and is
        then delivered to that batch."""
        res = self.as_(self.uploader).patch(
            self.url(), {"batch_id": str(self.foreign_batch.id)},
            format="json")
        self.assertEqual(res.status_code, 400)
        self.material.refresh_from_db()
        self.assertIsNone(self.material.batch_id)

    def test_blank_title_is_refused(self):
        res = self.as_(self.uploader).patch(
            self.url(), {"title": "   "}, format="json")
        self.assertEqual(res.status_code, 400)
        self.material.refresh_from_db()
        self.assertEqual(self.material.title, "Notes")

    def test_title_is_trimmed(self):
        self.as_(self.uploader).patch(
            self.url(), {"title": "  Spaced  "}, format="json")
        self.material.refresh_from_db()
        self.assertEqual(self.material.title, "Spaced")

    # ------------------------------------------------------------------
    # what must NOT be editable
    # ------------------------------------------------------------------

    def test_subject_cannot_be_changed(self):
        """Silently ignored, not honoured: subject is the authorization anchor
        and the editor check already ran against the OLD one."""
        self.as_(self.uploader).patch(
            self.url(), {"subject": str(self.other_subject.id),
                         "subject_id": str(self.other_subject.id)},
            format="json")
        self.material.refresh_from_db()
        self.assertEqual(self.material.subject_id, self.subject.id)

    def test_uploader_cannot_be_reassigned_via_the_body(self):
        self.as_(self.coteacher).patch(
            self.url(), {"uploaded_by": str(self.coteacher.id)},
            format="json")
        self.material.refresh_from_db()
        self.assertEqual(self.material.uploaded_by_id, self.uploader.id)

    def test_patch_does_not_attach_files(self):
        """Attachments have exactly one validated write path. A PATCH that
        carries a file must not become a second one."""
        before = self.material.files.count()
        self.as_(self.uploader).patch(
            self.url(), {"title": "x", "files": "anything"}, format="json")
        self.material.refresh_from_db()
        self.assertEqual(self.material.files.count(), before)

    # ------------------------------------------------------------------
    # response shape
    # ------------------------------------------------------------------

    def test_response_is_the_full_material_shape(self):
        res = self.as_(self.uploader).patch(
            self.url(), {"title": "Shaped"}, format="json")
        body = res.json()
        # The caller updates its row from this rather than refetching, so it
        # has to carry everything the list rows carry.
        for key in ("id", "title", "description", "subject_id", "subject_name",
                    "batch_id", "chapter_id", "files",
                    "created_at", "updated_at"):
            self.assertIn(key, body)
        self.assertEqual(body["title"], "Shaped")

    def test_response_does_not_leak_the_uploader_identity(self):
        """This serializer is shared with four STUDENT-facing endpoints.

        uploaded_by_id/uploaded_by_name lived here briefly and were removed:
        they published a teacher's internal auth-user UUID to every enrolled
        student, and cost an extra query per row on a learner hot path. The hub
        resolves ownership server-side instead. See StudyMaterialSerializer.
        """
        body = self.as_(self.uploader).patch(
            self.url(), {"title": "Shaped"}, format="json"
        ).json()
        self.assertNotIn("uploaded_by_id", body)
        self.assertNotIn("uploaded_by_name", body)
