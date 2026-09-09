"""Cover for an admin uploading study material on a teacher's behalf.

Two properties matter here and both are silent when broken:

  * the material must be OWNED BY THE TEACHER, not by the admin. An
    admin-owned row is visible to students and absent from every teacher
    screen, because those screens scope by TeachingAssignment and filter
    `mine` on `uploaded_by`. Nobody would look after it.
  * every guard the teacher path runs must still run, against the CHOSEN
    TEACHER. Relaxing the class-level teacher-context gate to let an admin in
    is exactly the kind of change that quietly takes the staffing check with
    it, and the result is an admin able to attach content to any subject in the
    catalogue under any teacher's name.

The regression half of this file is therefore as important as the new
behaviour: a teacher's own upload, and the learner-context token that must
still be refused on a teacher's account.
"""

from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User, UserRole
from courses.models import Batch, Chapter, Course, Subject, TeachingAssignment
from materials.models import MaterialFile, StudyMaterial

UPLOAD = "/api/materials/materials/upload/"
TEMP = "/api/materials/files/upload/"


class AdminMaterialUploadTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")

        cls.admin = User.objects.create_user(
            username="boss", email="boss@test.com", password="x",
            is_staff=True, first_name="Ada", last_name="Boss")
        cls.teacher = User.objects.create_user(
            username="t1", email="t1@test.com", password="x",
            first_name="Tara", last_name="One")
        cls.outsider = User.objects.create_user(
            username="t2", email="t2@test.com", password="x",
            first_name="Bala", last_name="Two")
        cls.not_a_teacher = User.objects.create_user(
            username="s1", email="s1@test.com", password="x")
        for u in (cls.teacher, cls.outsider):
            UserRole.objects.create(
                user=u, role=cls.teacher_role, is_active=True, is_primary=True)

        cls.course = Course.objects.create(title="Physics")
        cls.subject = Subject.objects.create(course=cls.course, name="Mechanics")
        cls.chapter = Chapter.objects.create(
            subject=cls.subject, title="Kinematics")
        cls.batch = Batch.objects.create(
            course=cls.course, name="Batch A", code="A1", year=2026)
        cls.foreign_course = Course.objects.create(title="History")
        cls.foreign_batch = Batch.objects.create(
            course=cls.foreign_course, name="Hist A", code="H1", year=2026)

        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, is_active=True,
            role=TeachingAssignment.ROLE_PRIMARY)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def client_for(self, user, context="teacher"):
        c = APIClient()
        c.force_authenticate(user=user, token={"context": context})
        return c

    def admin_client(self):
        # An admin's token carries no teacher context — there is no CTX_ADMIN.
        return self.client_for(self.admin, context="account")

    def temp_file(self, owner):
        return MaterialFile.objects.create(
            file="study_materials/notes.txt", material=None, uploaded_by=owner)

    # ------------------------------------------------------------------
    # the new path
    # ------------------------------------------------------------------

    def test_admin_upload_is_owned_by_the_chosen_teacher(self):
        f = self.temp_file(self.admin)
        res = self.admin_client().post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "teacher_id": str(self.teacher.id),
            "title": "Filed for Tara",
            "file_ids": [str(f.id)],
        })
        self.assertEqual(res.status_code, 201, res.data)

        material = StudyMaterial.objects.get(title="Filed for Tara")
        self.assertEqual(material.uploaded_by_id, self.teacher.id)
        self.assertEqual(material.subject_id, self.subject.id)
        # The file the ADMIN uploaded was still claimed. Matching the claim on
        # the chosen teacher instead would 404 every temp row.
        f.refresh_from_db()
        self.assertEqual(f.material_id, material.id)

    def test_admin_upload_appears_in_that_teachers_hub_as_theirs(self):
        f = self.temp_file(self.admin)
        self.admin_client().post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "teacher_id": str(self.teacher.id),
            "title": "Handover",
            "file_ids": [str(f.id)],
        })
        res = self.client_for(self.teacher).get(
            "/api/dashboard/teacher/resources/", {"mine": "true"})
        self.assertEqual(res.status_code, 200)
        rows = [r for r in res.data["results"] if r["title"] == "Handover"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_mine"])
        self.assertEqual(rows[0]["owner_name"], "Tara One")

    def test_admin_can_pick_a_chapter_and_a_batch(self):
        f = self.temp_file(self.admin)
        res = self.admin_client().post(UPLOAD, {
            "chapter_id": str(self.chapter.id),
            "teacher_id": str(self.teacher.id),
            "batch_id": str(self.batch.id),
            "title": "Chaptered",
            "file_ids": [str(f.id)],
        })
        self.assertEqual(res.status_code, 201, res.data)
        material = StudyMaterial.objects.get(title="Chaptered")
        self.assertEqual(material.chapter_id, self.chapter.id)
        self.assertEqual(material.batch_id, self.batch.id)

    def test_admin_can_mint_a_chapter_credited_to_the_teacher(self):
        f = self.temp_file(self.admin)
        res = self.admin_client().post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "teacher_id": str(self.teacher.id),
            "custom_chapter": "Rotational Motion",
            "title": "New chapter",
            "file_ids": [str(f.id)],
        })
        self.assertEqual(res.status_code, 201, res.data)
        chapter = Chapter.objects.get(title="Rotational Motion")
        # created_by is the TEACHER. A chapter credited to the admin would put
        # an account with no teaching role into the authorship of curriculum.
        self.assertEqual(chapter.created_by_id, self.teacher.id)

    def test_admin_can_upload_a_temp_file(self):
        # This was the first hard blocker: UploadTempFile was gated on the
        # teacher-context claim, so an admin could not upload the bytes at all.
        res = self.admin_client().post(
            TEMP,
            {"file": SimpleUploadedFile(
                "notes.txt", b"hello", content_type="text/plain")},
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            MaterialFile.objects.get(id=res.data["id"]).uploaded_by_id,
            self.admin.id,
        )

    def test_admin_temp_upload_still_runs_the_validator(self):
        res = self.admin_client().post(
            TEMP,
            {"file": SimpleUploadedFile(
                "payload.exe", b"MZ", content_type="application/octet-stream")},
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)

    # ------------------------------------------------------------------
    # what the admin path must still refuse
    # ------------------------------------------------------------------

    def test_admin_cannot_file_under_a_teacher_who_does_not_teach_it(self):
        f = self.temp_file(self.admin)
        res = self.admin_client().post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "teacher_id": str(self.outsider.id),
            "title": "Wrong hands",
            "file_ids": [str(f.id)],
        })
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        # Names the teacher, so the admin can act on it. The teacher-facing
        # wording ("You are not assigned…") would be nonsense here.
        self.assertIn("Bala Two", str(res.data))
        self.assertFalse(StudyMaterial.objects.filter(title="Wrong hands").exists())

    def test_admin_must_name_a_teacher(self):
        f = self.temp_file(self.admin)
        res = self.admin_client().post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "title": "Ownerless",
            "file_ids": [str(f.id)],
        })
        # 400, not 403. With no teacher named the request means "file this
        # under myself", and a pure admin has no teacher profile to switch to —
        # so the teacher-context message would be advice they cannot follow.
        # Naming somebody is a form error.
        self.assertEqual(res.status_code, 400)
        self.assertIn("teacher_id", res.data)
        self.assertFalse(StudyMaterial.objects.filter(title="Ownerless").exists())

    def test_teacher_id_must_be_a_teacher(self):
        f = self.temp_file(self.admin)
        res = self.admin_client().post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "teacher_id": str(self.not_a_teacher.id),
            "title": "Not staff",
            "file_ids": [str(f.id)],
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("teacher_id", res.data)

    def test_unknown_teacher_id_is_a_400(self):
        f = self.temp_file(self.admin)
        res = self.admin_client().post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "teacher_id": "11111111-1111-1111-1111-111111111111",
            "title": "Ghost",
            "file_ids": [str(f.id)],
        })
        self.assertEqual(res.status_code, 400)

    def test_malformed_teacher_id_is_a_400_not_a_500(self):
        f = self.temp_file(self.admin)
        res = self.admin_client().post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "teacher_id": "nope",
            "title": "Junk",
            "file_ids": [str(f.id)],
        })
        self.assertEqual(res.status_code, 400)

    def test_a_teacher_cannot_file_under_someone_else(self):
        # The escalation this whole design has to refuse: `teacher_id` is not a
        # general-purpose attribution field, it is an admin-only one.
        f = self.temp_file(self.teacher)
        res = self.client_for(self.teacher).post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "teacher_id": str(self.outsider.id),
            "title": "Framed",
            "file_ids": [str(f.id)],
        })
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("admin", str(res.data).lower())

    def test_admin_batch_must_belong_to_the_subjects_course(self):
        f = self.temp_file(self.admin)
        res = self.admin_client().post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "teacher_id": str(self.teacher.id),
            "batch_id": str(self.foreign_batch.id),
            "title": "Wrong course",
            "file_ids": [str(f.id)],
        })
        # 404 from the get_object_or_404 that pins the batch to the subject's
        # course — a foreign batch would make the material invisible to every
        # student while the teacher's list showed it uploaded.
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    # ------------------------------------------------------------------
    # regressions: the teacher path must be untouched
    # ------------------------------------------------------------------

    def test_teacher_upload_is_unchanged(self):
        f = self.temp_file(self.teacher)
        res = self.client_for(self.teacher).post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "title": "My own notes",
            "file_ids": [str(f.id)],
        })
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            StudyMaterial.objects.get(title="My own notes").uploaded_by_id,
            self.teacher.id,
        )

    def test_learner_context_on_a_teacher_account_is_still_refused(self):
        # The Step-2B password gate. Dropping IsTeacherContext from the class
        # must not drop this — a child on a shared device holds a
        # learner-context token on the parent teacher's account.
        f = self.temp_file(self.teacher)
        res = self.client_for(self.teacher, context="learner").post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "title": "Shared device",
            "file_ids": [str(f.id)],
        })
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        res = self.client_for(self.teacher, context="learner").post(
            TEMP,
            {"file": SimpleUploadedFile("x.txt", b"hi", content_type="text/plain")},
            format="multipart",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_staff_teacher_in_learner_context_is_still_refused(self):
        # The bypass this design nearly shipped. `if not is_staff:
        # require_teacher_context(...)` looks equivalent to the old class gate
        # and is not: an account that is BOTH staff and a teacher skips it, and
        # a `teacher_id` naming ITSELF skips it again on the other endpoint.
        # The Step-2B gate exists for a learner-context token on a teacher's
        # own account (a child on a shared device); being an admin does not
        # make that token safe.
        staff_teacher = User.objects.create_user(
            username="both", email="both@test.com", password="x",
            is_staff=True, first_name="Dev", last_name="Both")
        UserRole.objects.create(
            user=staff_teacher, role=self.teacher_role,
            is_active=True, is_primary=True)
        TeachingAssignment.objects.create(
            subject=self.subject, teacher=staff_teacher, batch=self.batch,
            is_active=True, role=TeachingAssignment.ROLE_ASSISTANT)

        learner = self.client_for(staff_teacher, context="learner")
        self.assertEqual(
            learner.post(
                TEMP,
                {"file": SimpleUploadedFile(
                    "x.txt", b"hi", content_type="text/plain")},
                format="multipart",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        f = self.temp_file(staff_teacher)
        self.assertEqual(
            learner.post(UPLOAD, {
                "subject_id": str(self.subject.id),
                "teacher_id": str(staff_teacher.id),
                "title": "Shared device, staff edition",
                "file_ids": [str(f.id)],
            }).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertFalse(
            StudyMaterial.objects.filter(title__contains="Shared device").exists())

        # …and the same account in TEACHER context works, so the gate is a
        # gate and not a ban.
        self.assertEqual(
            self.client_for(staff_teacher).post(UPLOAD, {
                "subject_id": str(self.subject.id),
                "title": "My own, as a staff teacher",
                "file_ids": [str(self.temp_file(staff_teacher).id)],
            }).status_code,
            201,
        )

    def test_a_deactivated_teacher_cannot_be_named(self):
        # Their TeachingAssignment outlives the deactivation, so teaches_subject
        # accepts them — and they cannot log in to look after what they own,
        # which is the exact outcome filing-under-a-teacher exists to prevent.
        gone = User.objects.create_user(
            username="gone", email="gone@test.com", password="x",
            first_name="Gone", last_name="Away")
        UserRole.objects.create(
            user=gone, role=self.teacher_role, is_active=True, is_primary=True)
        TeachingAssignment.objects.create(
            subject=self.subject, teacher=gone, batch=self.batch,
            is_active=True, role=TeachingAssignment.ROLE_SUBSTITUTE)
        gone.is_active = False
        gone.save(update_fields=["is_active"])

        res = self.admin_client().post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "teacher_id": str(gone.id),
            "title": "Filed under nobody",
            "file_ids": [str(self.temp_file(self.admin).id)],
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("teacher_id", res.data)

    def test_a_student_cannot_reach_either_endpoint(self):
        # The regression that would matter most. Both endpoints lost
        # IsTeacherContext from permission_classes, and that class gate was
        # ALSO what kept out anyone without the TEACHER role. The replacement
        # has to keep doing that, for both endpoints, with and without a
        # teacher_id in the body.
        student = self.client_for(self.not_a_teacher, context="learner")
        f = self.temp_file(self.not_a_teacher)

        self.assertEqual(
            student.post(
                TEMP,
                {"file": SimpleUploadedFile(
                    "x.txt", b"hi", content_type="text/plain")},
                format="multipart",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            student.post(UPLOAD, {
                "subject_id": str(self.subject.id),
                "title": "Student upload",
                "file_ids": [str(f.id)],
            }).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        # And naming a real teacher does not help them.
        self.assertEqual(
            student.post(UPLOAD, {
                "subject_id": str(self.subject.id),
                "teacher_id": str(self.teacher.id),
                "title": "Student upload 2",
                "file_ids": [str(f.id)],
            }).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertFalse(
            StudyMaterial.objects.filter(title__startswith="Student").exists())

    def test_an_anonymous_caller_cannot_reach_either_endpoint(self):
        anon = APIClient()
        self.assertEqual(anon.post(TEMP, {}, format="multipart").status_code, 401)
        self.assertEqual(anon.post(UPLOAD, {}).status_code, 401)

    def test_unassigned_teacher_is_still_refused(self):
        f = self.temp_file(self.outsider)
        res = self.client_for(self.outsider).post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "title": "Sneaky",
            "file_ids": [str(f.id)],
        })
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_still_cannot_claim_a_teachers_temp_file(self):
        # The claim is scoped to request.user, so an admin cannot re-parent a
        # file some teacher uploaded — the same theft this was closed against
        # between two teachers.
        stolen = self.temp_file(self.outsider)
        res = self.admin_client().post(UPLOAD, {
            "subject_id": str(self.subject.id),
            "teacher_id": str(self.teacher.id),
            "title": "Grab",
            "file_ids": [str(stolen.id)],
        })
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
