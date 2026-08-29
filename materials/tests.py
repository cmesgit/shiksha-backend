"""
materials — teacher write endpoints must require teacher CONTEXT (not just an
authenticated session), and delete is restricted to the uploading teacher.
Previously upload/delete were only IsAuthenticated → any account (incl. a child
on a shared device) could upload or delete study material.
"""
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import User, Role, UserRole, LearnerProfile
from courses.models import Course, Subject, Chapter
from materials.models import StudyMaterial


class MaterialsDeleteGateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher_role = Role.objects.create(name="TEACHER")
        cls.student_role = Role.objects.create(name="STUDENT")

        cls.teacher = User.objects.create_user(username="t1", email="t1@test.com", password="x")
        UserRole.objects.create(user=cls.teacher, role=cls.teacher_role, is_active=True, is_primary=True)
        cls.other_teacher = User.objects.create_user(username="t2", email="t2@test.com", password="x")
        UserRole.objects.create(user=cls.other_teacher, role=cls.teacher_role, is_active=True, is_primary=True)

        cls.child_account = User.objects.create_user(username="kid", email="kid@test.com", password="x")
        UserRole.objects.create(user=cls.child_account, role=cls.student_role, is_active=True, is_primary=True)
        cls.child = LearnerProfile.objects.create(account=cls.child_account, display_name="Kid", is_default=True)

        cls.course = Course.objects.create(title="Physics")
        cls.subject = Subject.objects.create(course=cls.course, name="Mechanics")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="Kinematics")
        cls.material = StudyMaterial.objects.create(chapter=cls.chapter, title="Notes",
                                                    uploaded_by=cls.teacher)

    def url(self):
        return f"/api/materials/materials/{self.material.id}/delete/"

    def client_with(self, user, context):
        c = APIClient()
        token = {"context": context}
        if context == "learner":
            token["active_profile"] = str(self.child.id)
        c.force_authenticate(user=user, token=token)
        return c

    def test_learner_context_cannot_delete(self):
        res = self.client_with(self.child_account, "learner").delete(self.url())
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(StudyMaterial.objects.filter(id=self.material.id).exists())

    def test_other_teacher_cannot_delete(self):
        res = self.client_with(self.other_teacher, "teacher").delete(self.url())
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(StudyMaterial.objects.filter(id=self.material.id).exists())

    def test_uploading_teacher_can_delete(self):
        res = self.client_with(self.teacher, "teacher").delete(self.url())
        self.assertIn(res.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        self.assertFalse(StudyMaterial.objects.filter(id=self.material.id).exists())


class MaterialsSubscriptionGateTest(TestCase):
    """ChapterMaterials/SubjectMaterials/StudyMaterialDetail were
    IsAuthenticated with no subscription or enrollment check at all — any
    authenticated account (a free signup, no subscription) could enumerate
    subject/chapter/material UUIDs and read (and download, via file_url)
    every paid course's material. Regression cover for the fix requiring a
    teaching assignment or an active subscription."""

    @classmethod
    def setUpTestData(cls):
        from django.utils import timezone
        from datetime import timedelta
        from enrollments.models import Subscription
        from courses.models import TeachingAssignment, Batch

        teacher_role = Role.objects.create(name="TEACHER")
        student_role = Role.objects.create(name="STUDENT")

        cls.teacher = User.objects.create_user(username="mg_t", email="mg_t@test.com", password="x")
        UserRole.objects.create(user=cls.teacher, role=teacher_role, is_active=True, is_primary=True)

        cls.paying_account = User.objects.create_user(username="mg_pay", email="mg_pay@test.com", password="x")
        UserRole.objects.create(user=cls.paying_account, role=student_role, is_active=True, is_primary=True)
        cls.paying_profile = LearnerProfile.objects.create(
            account=cls.paying_account, display_name="Payer", is_default=True
        )

        cls.freeloader_account = User.objects.create_user(username="mg_free", email="mg_free@test.com", password="x")
        UserRole.objects.create(user=cls.freeloader_account, role=student_role, is_active=True, is_primary=True)
        cls.freeloader_profile = LearnerProfile.objects.create(
            account=cls.freeloader_account, display_name="Freeloader", is_default=True
        )

        cls.course = Course.objects.create(title="Paid Course")
        cls.subject = Subject.objects.create(course=cls.course, name="Mechanics")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="Kinematics")
        cls.material = StudyMaterial.objects.create(
            chapter=cls.chapter, title="Paid Notes", uploaded_by=cls.teacher
        )

        cls.batch = Batch.objects.create(course=cls.course, name="Batch 1", code="B1")
        TeachingAssignment.objects.create(
            batch=cls.batch, subject=cls.subject, teacher=cls.teacher,
        )
        Subscription.objects.create(
            user=cls.paying_account, learner_profile=cls.paying_profile,
            course=cls.course, status=Subscription.STATUS_ACTIVE,
            starts_at=timezone.now(), expires_at=timezone.now() + timedelta(days=30),
        )

    def client_with(self, user, context, profile=None):
        c = APIClient()
        token = {"context": context}
        if profile is not None:
            token["active_profile"] = str(profile.id)
        c.force_authenticate(user=user, token=token)
        return c

    def test_freeloader_cannot_read_subject_materials(self):
        c = self.client_with(self.freeloader_account, "learner", self.freeloader_profile)
        res = c.get(f"/api/materials/subjects/{self.subject.id}/materials/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_freeloader_cannot_read_material_detail(self):
        c = self.client_with(self.freeloader_account, "learner", self.freeloader_profile)
        res = c.get(f"/api/materials/materials/{self.material.id}/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_paying_student_can_read_subject_materials(self):
        c = self.client_with(self.paying_account, "learner", self.paying_profile)
        res = c.get(f"/api/materials/subjects/{self.subject.id}/materials/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_assigned_teacher_can_read_subject_materials(self):
        c = self.client_with(self.teacher, "teacher")
        res = c.get(f"/api/materials/subjects/{self.subject.id}/materials/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_unassigned_teacher_cannot_upload_to_this_chapter(self):
        other_teacher = User.objects.create_user(username="mg_t2", email="mg_t2@test.com", password="x")
        UserRole.objects.create(
            user=other_teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        c = self.client_with(other_teacher, "teacher")
        res = c.post(
            "/api/materials/materials/upload/",
            {"chapter_id": str(self.chapter.id), "title": "Sneaky", "file_ids": []},
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_claim_another_teachers_uploaded_file(self):
        """MaterialFile.uploaded_by existed on the model but was never set at
        upload time nor checked at attach time — any teacher could attach
        (steal) a file another teacher had just uploaded, by guessing or
        reading its UUID."""
        from materials.models import MaterialFile
        import io

        other_teacher = User.objects.create_user(username="mg_t3", email="mg_t3@test.com", password="x")
        UserRole.objects.create(
            user=other_teacher, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True,
        )
        stolen = MaterialFile.objects.create(
            file="study_materials/stolen.txt", material=None, uploaded_by=other_teacher,
        )
        c = self.client_with(self.teacher, "teacher")
        res = c.post(
            "/api/materials/materials/upload/",
            {"chapter_id": str(self.chapter.id), "title": "Grab", "file_ids": [str(stolen.id)]},
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)


class SecureMediaViewTest(TestCase):
    """The /media/ alias used to be served by nginx with zero auth — any
    file, once its path was known, was permanently and anonymously
    downloadable regardless of the owning API's own permission check.
    Files now resolve through /api/media/secure/<path>, which re-runs the
    same authorization the API endpoint would (config.media_security).
    settings_test sets MEDIA_SERVED_BY_NGINX=False, so an authorized
    request gets the actual bytes back directly instead of an
    X-Accel-Redirect header nginx isn't present to resolve."""

    @classmethod
    def setUpTestData(cls):
        from django.core.files.base import ContentFile
        from courses.models import TeachingAssignment, Batch
        from materials.models import MaterialFile

        cls.teacher = User.objects.create_user(username="sm_t", email="sm_t@test.com", password="x")
        UserRole.objects.create(
            user=cls.teacher, role=Role.objects.create(name="SM_TEACHER"),
            is_active=True, is_primary=True,
        )
        cls.other_teacher = User.objects.create_user(username="sm_t2", email="sm_t2@test.com", password="x")
        UserRole.objects.create(
            user=cls.other_teacher, role=Role.objects.get(name="SM_TEACHER"),
            is_active=True, is_primary=True,
        )

        cls.course = Course.objects.create(title="Secure Media Course")
        cls.subject = Subject.objects.create(course=cls.course, name="SM Subject")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="SM Chapter")
        batch = Batch.objects.create(course=cls.course, name="B1", code="SM1")
        TeachingAssignment.objects.create(batch=batch, subject=cls.subject, teacher=cls.teacher)

        cls.material = StudyMaterial.objects.create(chapter=cls.chapter, title="Notes", uploaded_by=cls.teacher)
        cls.file = MaterialFile.objects.create(
            # FileField's own upload_to ("study_materials/") already
            # prefixes whatever name is given here — don't prefix it again.
            file=ContentFile(b"secret pdf bytes", name="secure_test.txt"),
            material=cls.material,
        )

    def url(self):
        return f"/api/media/secure/{self.file.file.name}"

    def test_assigned_teacher_can_read_the_bytes(self):
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        res = c.get(self.url())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(b"".join(res.streaming_content), b"secret pdf bytes")

    def test_unassigned_teacher_gets_404_not_the_file(self):
        c = APIClient()
        c.force_authenticate(user=self.other_teacher, token={"context": "teacher"})
        res = c.get(self.url())
        self.assertEqual(res.status_code, 404)

    def test_unauthenticated_request_gets_404(self):
        c = APIClient()
        res = c.get(self.url())
        self.assertEqual(res.status_code, 404)

    def test_unmapped_prefix_denies_by_default(self):
        """A path with no registered check (e.g. something under forum/ or
        documents/ — see MEDIA_SECURITY_TODO.md) must deny non-staff by
        default, never fall open."""
        c = APIClient()
        c.force_authenticate(user=self.teacher, token={"context": "teacher"})
        res = c.get("/api/media/secure/forum/whatever.png")
        self.assertEqual(res.status_code, 404)

    def test_public_prefix_is_not_routed_through_this_view_at_all(self):
        """SecureLocalStorage.url() must return the plain /media/ path for
        public zones — confirms the storage-level branch, not just the
        view's own behavior."""
        from django.core.files.storage import default_storage
        url = default_storage.url("content/blog/cover.jpg")
        self.assertTrue(url.startswith("/media/"))
        self.assertNotIn("/api/media/secure/", url)

    def test_private_prefix_url_points_at_the_secure_endpoint(self):
        from django.core.files.storage import default_storage
        url = default_storage.url(self.file.file.name)
        self.assertTrue(url.startswith("/api/media/secure/"))

    def test_public_teacher_photo_does_not_leak_a_sibling_private_prefix(self):
        """Regression cover: bare 'teachers/' (the public bio photo) and
        'teachers/certificates/' (private KYC doc) share a parent
        directory. An earlier version of this module checked "is it under
        any public prefix" and "is it under any private prefix" as two
        independent passes — name.startswith(PUBLIC_PREFIXES) matched
        'teachers/certificates/x.pdf' against the public 'teachers/'
        prefix and returned public before the private check ever ran,
        which would have exposed every teacher's KYC documents the moment
        the bio-photo prefix was added. Both must be one length-ordered
        table now."""
        from config.media_security import is_public, is_authorized
        from django.test import RequestFactory

        self.assertTrue(is_public("teachers/photo123.jpg"))
        self.assertFalse(is_public("teachers/certificates/cert1.pdf"))
        self.assertFalse(is_public("teachers/id_proofs/front.jpg"))

        rf = RequestFactory()
        req = rf.get("/")
        req.user = self.other_teacher
        self.assertFalse(is_authorized(req, "teachers/certificates/cert1.pdf"))


class MaterialsDeleteAuthorityTest(TestCase):
    """Deletion authority used to be INCOHERENT between the two content
    types, and it contradicted the list the button is rendered from.

    /materials/teacher/materials/all/ returns every material on the
    teacher's subjects, colleagues' included, each with a Delete action —
    but DeleteStudyMaterial required `uploaded_by`, so on a colleague's row
    it could only ever 403. Recordings meanwhile let ANY co-teacher delete
    (and destroy the Bunny asset). One rule now: subject teaching staff, or
    an admin. Cover for both the widening and the boundary that did NOT
    move — a teacher with no assignment on the subject still cannot touch it.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import TeachingAssignment

        teacher_role = Role.objects.create(name="TEACHER")
        student_role = Role.objects.create(name="STUDENT")

        cls.course = Course.objects.create(title="Chemistry")
        cls.subject = Subject.objects.create(course=cls.course, name="Organic")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="Alkanes")

        cls.uploader = User.objects.create_user(username="da_up", email="da_up@t.com", password="x")
        UserRole.objects.create(user=cls.uploader, role=teacher_role, is_active=True, is_primary=True)
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.uploader, batch=None, is_active=True,
        )

        # A COLLEAGUE on the same subject — the case the frontend showed a
        # dead Delete button for. role=ASSISTANT: two active PRIMARY rows on
        # the same course-wide subject would trip
        # courses.models.TeachingAssignment's "one active PRIMARY per
        # subject" constraint.
        cls.colleague = User.objects.create_user(username="da_col", email="da_col@t.com", password="x")
        UserRole.objects.create(user=cls.colleague, role=teacher_role, is_active=True, is_primary=True)
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.colleague, batch=None, is_active=True,
            role=TeachingAssignment.ROLE_ASSISTANT,
        )

        # A teacher with no assignment on this subject at all.
        cls.stranger = User.objects.create_user(username="da_str", email="da_str@t.com", password="x")
        UserRole.objects.create(user=cls.stranger, role=teacher_role, is_active=True, is_primary=True)

        cls.learner_account = User.objects.create_user(username="da_kid", email="da_kid@t.com", password="x")
        UserRole.objects.create(user=cls.learner_account, role=student_role, is_active=True, is_primary=True)
        cls.learner = LearnerProfile.objects.create(
            account=cls.learner_account, display_name="Kid", is_default=True,
        )

    def setUp(self):
        self.material = StudyMaterial.objects.create(
            chapter=self.chapter, title="Notes", uploaded_by=self.uploader,
        )

    def client_with(self, user, context, profile=None):
        c = APIClient()
        token = {"context": context}
        if profile is not None:
            token["active_profile"] = str(profile.id)
        c.force_authenticate(user=user, token=token)
        return c

    def url(self):
        return f"/api/materials/materials/{self.material.id}/delete/"

    def test_colleague_on_the_same_subject_can_delete(self):
        res = self.client_with(self.colleague, "teacher").delete(self.url())
        self.assertIn(res.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        self.assertFalse(StudyMaterial.objects.filter(id=self.material.id).exists())

    def test_teacher_not_assigned_to_the_subject_still_cannot_delete(self):
        res = self.client_with(self.stranger, "teacher").delete(self.url())
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(StudyMaterial.objects.filter(id=self.material.id).exists())

    def test_learner_context_still_cannot_delete(self):
        res = self.client_with(self.learner_account, "learner", self.learner).delete(self.url())
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(StudyMaterial.objects.filter(id=self.material.id).exists())


class MaterialsAdminAccessTest(TestCase):
    """An admin could not see, open or delete ANY study material.

    `DeleteStudyMaterial` was gated on `IsTeacherContext`, which requires the
    TEACHER role AND a teacher JWT context claim, so a pure staff account was
    rejected at the class gate and the `request.user.is_staff` branch in the
    body was unreachable dead code. `_authorize_subject_materials` had no
    staff branch either, so reads fell through to the subscription check and
    were denied too.

    Net effect, verified live against a running server before the fix:
        admin GET    material detail -> 403
        admin DELETE material        -> 403
        admin GET    subject list    -> 403
    A material went live to students the instant a teacher uploaded it and no
    admin could take it down through the API at all. Recordings had the
    identical bug and were fixed first; this is the same rule.
    """

    @classmethod
    def setUpTestData(cls):
        from courses.models import TeachingAssignment

        teacher_role = Role.objects.create(name="TEACHER")
        cls.course = Course.objects.create(title="Physics")
        cls.subject = Subject.objects.create(course=cls.course, name="Optics")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="Lenses")

        cls.teacher = User.objects.create_user(
            username="aa_t", email="aa_t@t.com", password="x")
        UserRole.objects.create(user=cls.teacher, role=teacher_role,
                                is_active=True, is_primary=True)
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, batch=None, is_active=True,
        )

        # A PURE admin: staff, no TEACHER role, no teaching assignment, no
        # learner profile. Every gate in this app used to reject them.
        cls.admin = User.objects.create_user(
            username="aa_admin", email="aa_admin@t.com", password="x",
            is_staff=True,
        )

    def setUp(self):
        self.material = StudyMaterial.objects.create(
            chapter=self.chapter, title="Notes", uploaded_by=self.teacher,
        )

    def client_with(self, user, context):
        c = APIClient()
        c.force_authenticate(user=user, token={"context": context})
        return c

    def test_admin_can_read_a_material_detail(self):
        res = self.client_with(self.admin, "admin").get(
            f"/api/materials/materials/{self.material.id}/"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_admin_can_list_a_subjects_materials(self):
        res = self.client_with(self.admin, "admin").get(
            f"/api/materials/subjects/{self.subject.id}/materials/"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_admin_can_delete_a_material(self):
        res = self.client_with(self.admin, "admin").delete(
            f"/api/materials/materials/{self.material.id}/delete/"
        )
        self.assertIn(res.status_code,
                      (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        self.assertFalse(
            StudyMaterial.objects.filter(id=self.material.id).exists()
        )

    def test_a_non_staff_stranger_is_still_denied(self):
        """The widening must be to STAFF, not to everyone authenticated."""
        stranger = User.objects.create_user(
            username="aa_x", email="aa_x@t.com", password="x")
        res = self.client_with(stranger, "learner").delete(
            f"/api/materials/materials/{self.material.id}/delete/"
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            StudyMaterial.objects.filter(id=self.material.id).exists()
        )

    def test_admin_sees_batch_scoped_material_regardless_of_batch(self):
        """An admin has no batch, so the batch gate must not silently hide
        the very content they are moderating."""
        from courses.models import Batch

        batch = Batch.objects.create(course=self.course, name="A", code="OA")
        self.material.batch = batch
        self.material.save()
        res = self.client_with(self.admin, "admin").get(
            f"/api/materials/materials/{self.material.id}/"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)


class MaterialsBatchValidationTest(TestCase):
    """MEDIUM — UploadStudyMaterial did get_object_or_404(Batch, id=batch_id)
    with no course check, so a batch from ANOTHER course was accepted. Every
    read path filters `batch__isnull=True | batch_id=<their batch>`, which
    can never match it, so the handout was invisible to every student while
    the teacher's list showed it uploaded — and _enrollments_for notified
    nobody."""

    @classmethod
    def setUpTestData(cls):
        from courses.models import Batch, TeachingAssignment

        teacher_role = Role.objects.create(name="TEACHER")
        cls.teacher = User.objects.create_user(username="bv_t", email="bv_t@t.com", password="x")
        UserRole.objects.create(user=cls.teacher, role=teacher_role, is_active=True, is_primary=True)

        cls.course = Course.objects.create(title="Biology")
        cls.subject = Subject.objects.create(course=cls.course, name="Genetics")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="Mendel")
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, batch=None, is_active=True,
        )
        cls.own_batch = Batch.objects.create(course=cls.course, name="Bio A", code="BA")

        cls.other_course = Course.objects.create(title="History")
        cls.foreign_batch = Batch.objects.create(
            course=cls.other_course, name="Hist A", code="HA",
        )

    def _file_id(self):
        from materials.models import MaterialFile
        return str(
            MaterialFile.objects.create(
                file="study_materials/x.txt", material=None, uploaded_by=self.teacher,
            ).id
        )

    def client_with(self, user):
        c = APIClient()
        c.force_authenticate(user=user, token={"context": "teacher"})
        return c

    def test_foreign_course_batch_is_rejected(self):
        res = self.client_with(self.teacher).post(
            "/api/materials/materials/upload/",
            {
                "chapter_id": str(self.chapter.id),
                "title": "Orphan handout",
                "batch_id": str(self.foreign_batch.id),
                "file_ids": [self._file_id()],
            },
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(StudyMaterial.objects.filter(title="Orphan handout").exists())

    def test_own_course_batch_is_accepted(self):
        res = self.client_with(self.teacher).post(
            "/api/materials/materials/upload/",
            {
                "chapter_id": str(self.chapter.id),
                "title": "Real handout",
                "batch_id": str(self.own_batch.id),
                "file_ids": [self._file_id()],
            },
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            StudyMaterial.objects.get(title="Real handout").batch_id, self.own_batch.id,
        )


class UnplacedLearnerBatchScopeTest(TestCase):
    """A learner with no batch was NOTIFIED about batch-scoped material and
    then structurally unable to see it.

    activity/signals.py's `_enrollments_for_batches` deliberately notifies
    enrollments with `batch IS NULL` about batch-scoped items — an unplaced
    learner gets "New study material: X". But both material readers built
    their filter as an unconditional

        Q(batch__isnull=True) | Q(batch_id=batch_id)

    and with batch_id None the right side compiles to `batch_id IS NULL`,
    identical to the left, so the OR collapsed to "course-wide only" and X
    could never appear. The learner clicked the notification and landed on
    "No material for this subject".

    assignments/views.py:322-326 already guarded this with
    `if batch_id is not None`; materials never copied it. These tests pin the
    two readers to the notifier's behaviour.
    """

    @classmethod
    def setUpTestData(cls):
        from django.utils import timezone
        from datetime import timedelta
        from enrollments.models import Subscription, Enrollment
        from courses.models import Batch, TeachingAssignment

        Role.objects.create(name="TEACHER")
        student_role = Role.objects.create(name="STUDENT")

        cls.teacher = User.objects.create_user(
            username="ub_t", email="ub_t@t.com", password="x")

        cls.account = User.objects.create_user(
            username="ub_s", email="ub_s@t.com", password="x")
        UserRole.objects.create(
            user=cls.account, role=student_role, is_active=True, is_primary=True)
        cls.profile = LearnerProfile.objects.create(
            account=cls.account, display_name="Unplaced", is_default=True)

        cls.course = Course.objects.create(title="Class 12 Science")
        cls.subject = Subject.objects.create(course=cls.course, name="Mathematics")
        cls.chapter = Chapter.objects.create(subject=cls.subject, title="Calculus")
        cls.batch = Batch.objects.create(
            course=cls.course, name="Morning", code="MB1")
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, batch=None, is_active=True)

        # The material the learner was notified about: scoped to a batch.
        cls.batch_material = StudyMaterial.objects.create(
            chapter=cls.chapter, subject=cls.subject, batch=cls.batch,
            title="cx dsv", uploaded_by=cls.teacher)
        # A course-wide one, which was always visible.
        cls.open_material = StudyMaterial.objects.create(
            chapter=cls.chapter, subject=cls.subject, batch=None,
            title="Course-wide notes", uploaded_by=cls.teacher)

        # Enrolled and paying, but never PLACED in a batch (batch=None) —
        # exactly what a self-enrolment produces before an admin sorts cohorts.
        Enrollment.objects.create(
            user=cls.account, learner_profile=cls.profile, course=cls.course,
            batch=None, status=Enrollment.STATUS_ACTIVE)
        Subscription.objects.create(
            user=cls.account, learner_profile=cls.profile, course=cls.course,
            status=Subscription.STATUS_ACTIVE,
            starts_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=30))

    def _client(self):
        c = APIClient()
        c.force_authenticate(
            user=self.account,
            token={"context": "learner", "active_profile": str(self.profile.id)})
        return c

    def test_unplaced_learner_sees_batch_scoped_material_in_the_course_list(self):
        res = self._client().get(
            f"/api/materials/student/courses/{self.course.id}/materials/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        titles = {row["title"] for row in res.data}
        self.assertIn(
            "cx dsv", titles,
            "the learner was notified about this material — it must be readable",
        )
        self.assertIn("Course-wide notes", titles)

    def test_unplaced_learner_sees_batch_scoped_material_in_the_subject_list(self):
        res = self._client().get(
            f"/api/materials/subjects/{self.subject.id}/materials/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        self.assertIn("cx dsv", {row["title"] for row in res.data})

    def test_a_placed_learner_still_cannot_read_another_batchs_material(self):
        """The fix must not become "batch scoping is off for everyone"."""
        from enrollments.models import Enrollment
        from courses.models import Batch

        other = Batch.objects.create(
            course=self.course, name="Evening", code="EB1")
        Enrollment.objects.filter(learner_profile=self.profile).update(batch=other)

        res = self._client().get(
            f"/api/materials/student/courses/{self.course.id}/materials/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        titles = {row["title"] for row in res.data}
        self.assertNotIn("cx dsv", titles, "Morning batch material must stay hidden")
        self.assertIn("Course-wide notes", titles)


class MaterialsCustomChapterTest(TestCase):
    """UploadStudyMaterial's custom_chapter branch used to do a bare
    Chapter.objects.create(subject=subject, title=custom_chapter) — a second
    upload with a repeat (or case-varied) chapter name hit
    unique_chapter_per_subject and 500'd instead of reusing the existing
    chapter. Now routed through courses.services.resolve_or_create_chapter()."""

    @classmethod
    def setUpTestData(cls):
        from courses.models import TeachingAssignment

        teacher_role = Role.objects.create(name="TEACHER")
        cls.teacher = User.objects.create_user(username="cc_t", email="cc_t@t.com", password="x")
        UserRole.objects.create(user=cls.teacher, role=teacher_role, is_active=True, is_primary=True)

        cls.course = Course.objects.create(title="Biology")
        cls.subject = Subject.objects.create(course=cls.course, name="Genetics")
        TeachingAssignment.objects.create(
            subject=cls.subject, teacher=cls.teacher, batch=None, is_active=True,
        )

    def _file_id(self):
        from materials.models import MaterialFile
        return str(
            MaterialFile.objects.create(
                file="study_materials/x.txt", material=None, uploaded_by=self.teacher,
            ).id
        )

    def client_with(self, user):
        c = APIClient()
        c.force_authenticate(user=user, token={"context": "teacher"})
        return c

    def _upload(self, title, custom_chapter):
        return self.client_with(self.teacher).post(
            "/api/materials/materials/upload/",
            {
                "custom_chapter": custom_chapter,
                "subject_id": str(self.subject.id),
                "title": title,
                "file_ids": [self._file_id()],
            },
        )

    def test_repeat_custom_chapter_name_reuses_the_existing_row(self):
        first = self._upload("Notes 1", "Mendelian Genetics")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.content)

        second = self._upload("Notes 2", "MENDELIAN GENETICS")
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.content)

        self.assertEqual(
            Chapter.objects.filter(subject=self.subject, title__iexact="Mendelian Genetics").count(),
            1,
        )
        self.assertEqual(
            StudyMaterial.objects.get(title="Notes 1").chapter_id,
            StudyMaterial.objects.get(title="Notes 2").chapter_id,
        )
