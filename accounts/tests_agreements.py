"""
Tests for the Agreement Letter CMS (accounts/agreement_views.py) and the
signature-binding helper (TeacherProfile.record_agreement_signature).

Previously zero test coverage existed for this feature at all. These cover
the fixes made alongside this test file: the version_number race guard
(select_for_update), restore not updating the letter title, the unknown-key
whitelist, the public endpoint no longer leaking an admin's email, and the
two upload paths that used to diverge into opposite bugs (one saved the file
and dropped the version, the other saved the version and dropped the file).
"""
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import User, AgreementLetter, AgreementLetterVersion, TeacherProfile


def _draft(letter):
    return letter.versions.filter(status=AgreementLetterVersion.STATUS_DRAFT).first()


class AgreementCMSTest(TestCase):
    PASSWORD = "s3cret-pass"

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="admin", email="admin@test.com", password=cls.PASSWORD,
            is_staff=True,
        )
        cls.other_admin = User.objects.create_user(
            username="admin2", email="admin2@test.com", password=cls.PASSWORD,
            is_staff=True,
        )
        cls.plain_user = User.objects.create_user(
            username="learner", email="learner@test.com", password=cls.PASSWORD,
        )

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def save_and_publish(self, c, **payload):
        """Save the draft then publish it — the two-step the CMS now requires.
        Most of these tests care about the PUBLISHED outcome, not the staging."""
        r1 = c.post("/api/accounts/admin/agreements/faculty/save/", payload,
                    format=payload.pop("_format", "json"))
        assert r1.status_code == status.HTTP_200_OK, r1.data
        r2 = c.post("/api/accounts/admin/agreements/faculty/publish/")
        assert r2.status_code == status.HTTP_200_OK, r2.data
        return r2

    # ---- Save / versioning ------------------------------------------------

    def test_save_stages_a_draft_and_does_not_go_live(self):
        """The core of the draft/publish split: saving must not change what
        an applicant sees."""
        res = self.client_for(self.admin).post(
            "/api/accounts/admin/agreements/faculty/save/",
            {"title": "Faculty Agreement", "body": "1. Engagement\n\nTerms."},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["saved"], "draft")
        letter = AgreementLetter.objects.get(key="faculty")
        self.assertIsNone(letter.current_version, "saving must not publish")
        self.assertIsNotNone(res.data["draft"])
        self.assertIsNone(res.data["draft"]["version_number"])
        # Still invisible to applicants.
        self.assertEqual(
            APIClient().get("/api/accounts/agreements/faculty/").status_code,
            status.HTTP_404_NOT_FOUND)

    def test_publish_makes_the_draft_live_as_v1(self):
        c = self.client_for(self.admin)
        self.save_and_publish(c, title="Faculty Agreement", body="1. Engagement\n\nTerms.")
        letter = AgreementLetter.objects.get(key="faculty")
        self.assertEqual(letter.current_version.version_number, 1)
        self.assertEqual(letter.title, "Faculty Agreement")
        self.assertIsNone(_draft(letter), "publishing consumes the draft")

    def test_repeated_saves_update_the_same_draft(self):
        c = self.client_for(self.admin)
        for body in ("first pass", "second pass", "third pass"):
            c.post("/api/accounts/admin/agreements/faculty/save/",
                   {"title": "Faculty Agreement", "body": body}, format="json")
        letter = AgreementLetter.objects.get(key="faculty")
        drafts = letter.versions.filter(status="draft")
        self.assertEqual(drafts.count(), 1, "must not spawn a draft per save")
        self.assertEqual(drafts.first().body, "third pass")
        # And no numbers were burned along the way.
        self.assertEqual(letter.versions.filter(status="published").count(), 0)

    def test_discard_draft_leaves_the_live_version_untouched(self):
        c = self.client_for(self.admin)
        self.save_and_publish(c, title="Live", body="live text")
        c.post("/api/accounts/admin/agreements/faculty/save/",
               {"title": "WIP", "body": "half-written clause"}, format="json")
        res = c.post("/api/accounts/admin/agreements/faculty/discard-draft/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        letter = AgreementLetter.objects.get(key="faculty")
        self.assertIsNone(_draft(letter))
        self.assertEqual(letter.current_version.body, "live text")
        self.assertEqual(letter.current_version.version_number, 1)

    def test_publish_with_no_draft_is_rejected(self):
        res = self.client_for(self.admin).post(
            "/api/accounts/admin/agreements/faculty/publish/")
        self.assertIn(res.status_code,
                      (status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND))

    def test_version_history_excludes_the_draft(self):
        c = self.client_for(self.admin)
        self.save_and_publish(c, title="Faculty Agreement", body="v1")
        c.post("/api/accounts/admin/agreements/faculty/save/",
               {"title": "Faculty Agreement", "body": "unpublished"}, format="json")
        rows = c.get("/api/accounts/admin/agreements/faculty/versions/").data
        self.assertEqual([r["version_number"] for r in rows], [1])
        self.assertTrue(all(not r["is_draft"] for r in rows))

    def test_publishing_twice_appends_an_immutable_version(self):
        c = self.client_for(self.admin)
        self.save_and_publish(c, title="Faculty Agreement", body="v1 body")
        self.save_and_publish(c, title="Faculty Agreement", body="v2 body")
        letter = AgreementLetter.objects.get(key="faculty")
        self.assertEqual(letter.current_version.version_number, 2)
        self.assertEqual(letter.versions.filter(status="published").count(), 2)
        v1 = letter.versions.get(version_number=1)
        self.assertEqual(v1.body, "v1 body")  # never mutated

    def test_save_accepts_an_imported_file_instead_of_a_body(self):
        # Body used to be unconditionally required, forcing an admin to retype
        # a lawyer-drafted PDF as markdown just to publish it.
        from django.core.files.uploadedfile import SimpleUploadedFile
        res = self.client_for(self.admin).post(
            "/api/accounts/admin/agreements/faculty/save/",
            {
                "title": "Faculty Agreement",
                "body": "",
                "document": SimpleUploadedFile(
                    "agreement.pdf", b"%PDF-1.4 real", content_type="application/pdf"),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        # Staged on the draft; publish to make it the live version.
        self.assertIsNotNone(res.data["draft"]["document_url"])
        self.client_for(self.admin).post("/api/accounts/admin/agreements/faculty/publish/")
        v = AgreementLetter.objects.get(key="faculty").current_version
        self.assertTrue(bool(v.document))

    def test_save_rejects_a_non_document_import(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        res = self.client_for(self.admin).post(
            "/api/accounts/admin/agreements/faculty/save/",
            {
                "title": "Faculty Agreement", "body": "terms",
                "document": SimpleUploadedFile(
                    "evil.pdf", b"<html><script>alert(1)</script></html>",
                    content_type="application/pdf"),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_save_still_rejects_neither_body_nor_file(self):
        res = self.client_for(self.admin).post(
            "/api/accounts/admin/agreements/faculty/save/",
            {"title": "Faculty Agreement", "body": "   "}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_restore_carries_the_imported_file_forward(self):
        # Restoring a file-based version must not silently downgrade it to a
        # body-only one — that would change what applicants actually sign.
        from django.core.files.uploadedfile import SimpleUploadedFile
        c = self.client_for(self.admin)
        c.post("/api/accounts/admin/agreements/faculty/save/",
               {"title": "With file", "body": "v1",
                "document": SimpleUploadedFile("a.pdf", b"%PDF-1.4 one",
                                               content_type="application/pdf")},
               format="multipart")
        c.post("/api/accounts/admin/agreements/faculty/publish/")
        v1_id = AgreementLetter.objects.get(key="faculty").current_version_id
        self.save_and_publish(c, title="Text only", body="v2")
        self.assertFalse(bool(AgreementLetter.objects.get(key="faculty").current_version.document))

        c.post(f"/api/accounts/admin/agreements/versions/{v1_id}/restore/")
        # Restore stages — the file must survive into the draft, and then live.
        letter = AgreementLetter.objects.get(key="faculty")
        self.assertTrue(bool(_draft(letter).document))
        c.post("/api/accounts/admin/agreements/faculty/publish/")
        restored = AgreementLetter.objects.get(key="faculty").current_version
        self.assertTrue(bool(restored.document))

    def test_public_endpoint_exposes_the_document_url(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        c = self.client_for(self.admin)
        c.post("/api/accounts/admin/agreements/faculty/save/",
               {"title": "Faculty Agreement", "body": "terms",
                "document": SimpleUploadedFile("a.pdf", b"%PDF-1.4 x",
                                               content_type="application/pdf")},
               format="multipart")
        c.post("/api/accounts/admin/agreements/faculty/publish/")
        res = APIClient().get("/api/accounts/agreements/faculty/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(res.data["current_version"]["document_url"])
        # Still must not leak the authoring admin.
        self.assertNotIn("created_by", res.data["current_version"])

    def test_save_rejects_unknown_key(self):
        res = self.client_for(self.admin).post(
            "/api/accounts/admin/agreements/not-a-real-key/save/",
            {"title": "x", "body": "y"}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(AgreementLetter.objects.filter(key="not-a-real-key").exists())

    def test_save_requires_admin(self):
        res = self.client_for(self.plain_user).post(
            "/api/accounts/admin/agreements/faculty/save/",
            {"title": "x", "body": "y"}, format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # ---- Restore -----------------------------------------------------------

    def test_restore_copies_body_and_title_into_new_version(self):
        c = self.client_for(self.admin)
        self.save_and_publish(c, title="Original Title", body="original body")
        letter = AgreementLetter.objects.get(key="faculty")
        v1_id = letter.current_version_id
        self.save_and_publish(c, title="Newer Title", body="newer body")

        res = c.post(f"/api/accounts/admin/agreements/versions/{v1_id}/restore/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        # Restore stages rather than publishing, so nothing is live yet.
        letter.refresh_from_db()
        self.assertEqual(letter.current_version.version_number, 2)
        c.post("/api/accounts/admin/agreements/faculty/publish/")

        letter.refresh_from_db()
        self.assertEqual(letter.current_version.version_number, 3)
        self.assertEqual(letter.current_version.body, "original body")
        # BUG-11: restore used to leave letter.title on the most recent save's
        # title instead of the restored version's — admin list and the
        # faculty-facing render disagreed about the letter's own name.
        self.assertEqual(letter.title, "Original Title")

    def test_restore_unknown_version_404s(self):
        import uuid
        res = self.client_for(self.admin).post(
            f"/api/accounts/admin/agreements/versions/{uuid.uuid4()}/restore/"
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    # ---- Public endpoint ----------------------------------------------------

    def test_public_endpoint_404s_when_unpublished(self):
        res = APIClient().get("/api/accounts/agreements/faculty/")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_public_endpoint_404s_for_unknown_key(self):
        res = APIClient().get("/api/accounts/agreements/not-a-real-key/")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_public_endpoint_never_leaks_admin_email(self):
        self.save_and_publish(self.client_for(self.admin),
                              title="Faculty Agreement", body="terms")
        res = APIClient().get("/api/accounts/agreements/faculty/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["current_version"]["body"], "terms")
        # BUG-10: this used to be _letter_dict(full=True), which includes
        # current_version.created_by — a real admin's email — with no auth
        # gate and no throttle at all.
        self.assertNotIn("created_by", res.data["current_version"])
        self.assertNotIn(self.admin.email, str(res.data))

    def test_public_endpoint_is_throttled(self):
        from django.core.cache import cache
        from accounts.agreement_views import AgreementPublicRateThrottle

        # settings_test disables every throttle scope (see its own comment,
        # and accounts/tests_lookup.py's LoginThrottleTest) — @override_settings
        # can't reach it since DRF binds THROTTLE_RATES as a class attribute
        # at import time, so re-enable just this one for this test.
        cache.clear()
        saved = dict(AgreementPublicRateThrottle.THROTTLE_RATES)
        AgreementPublicRateThrottle.THROTTLE_RATES["agreement_public"] = "60/hour"
        self.addCleanup(lambda: (
            AgreementPublicRateThrottle.THROTTLE_RATES.clear(),
            AgreementPublicRateThrottle.THROTTLE_RATES.update(saved),
        ))

        self.save_and_publish(self.client_for(self.admin),
                              title="Faculty Agreement", body="terms")
        statuses = set()
        for _ in range(70):
            res = APIClient().get("/api/accounts/agreements/faculty/")
            statuses.add(res.status_code)
        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, statuses)


class AgreementSignatureBindingTest(TestCase):
    """TeacherProfile.record_agreement_signature — the fix for BUG-2/3/4:
    the two upload paths used to diverge (file-no-version vs version-no-file);
    this is now the single place both call into."""

    PASSWORD = "s3cret-pass"

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="admin", email="admin@test.com", password=cls.PASSWORD,
            is_staff=True,
        )

    def _publish(self, body="v1 body"):
        """Save the draft AND publish it — these tests need a LIVE version to
        bind signatures against."""
        c = APIClient()
        c.force_authenticate(user=self.admin)
        c.post("/api/accounts/admin/agreements/faculty/save/",
               {"title": "Faculty Agreement", "body": body}, format="json")
        c.post("/api/accounts/admin/agreements/faculty/publish/")
        return AgreementLetter.objects.get(key="faculty").current_version

    def test_binds_current_version_and_timestamp_once(self):
        v1 = self._publish("v1 body")
        tp = TeacherProfile.objects.create(user=User.objects.create_user(
            username="t1", email="t1@test.com", password=self.PASSWORD,
        ))
        self.assertIsNone(tp.signed_agreement_version_id)
        tp.record_agreement_signature(signer_name="Jane Doe")
        self.assertEqual(tp.signed_agreement_version_id, v1.id)
        self.assertIsNotNone(tp.signed_agreement_at)
        self.assertEqual(tp.agreement_signer_name, "Jane Doe")

    def test_never_rebinds_once_signed(self):
        v1 = self._publish("v1 body")
        tp = TeacherProfile.objects.create(user=User.objects.create_user(
            username="t2", email="t2@test.com", password=self.PASSWORD,
        ))
        tp.record_agreement_signature()
        first_signed_at = tp.signed_agreement_at
        self._publish("v2 body")  # admin publishes a newer version afterwards

        tp.record_agreement_signature()  # a hypothetical second call
        self.assertEqual(tp.signed_agreement_version_id, v1.id)
        self.assertEqual(tp.signed_agreement_at, first_signed_at)

    def test_signup_can_supply_the_signed_agreement_and_binds_it(self):
        """The pre-approval upload path. Step 3 of faculty signup used to be
        acknowledge-only, promising a dashboard upload that a PENDING
        applicant could not actually reach (learner form at /form-fillup, 403
        not_approved on the teacher editor) — so signed_agreement was
        unsuppliable until after approval."""
        import base64
        v1 = self._publish("terms")
        pdf_b64 = base64.b64encode(b"%PDF-1.4 signed").decode()

        res = APIClient().post("/api/accounts/signup/", {
            "email": "applicant@test.com",
            "password": "s3cret-pass!23",
            "role": "TEACHER",
            "teacher_type": "FACULTY",
            "terms_accepted": True,
            "faculty_profile": {
                "highest_degree": "masters",
                "signed_agreement": {
                    "name": "signed.pdf",
                    "type": "application/pdf",
                    "data": pdf_b64,
                },
            },
        }, format="json")
        self.assertIn(res.status_code, (200, 201), res.data)

        tp = TeacherProfile.objects.get(user__email="applicant@test.com")
        self.assertTrue(bool(tp.signed_agreement), "the uploaded bytes were dropped")
        self.assertEqual(tp.signed_agreement_version_id, v1.id)
        self.assertIsNotNone(tp.signed_agreement_at)

    def test_signup_records_every_subject_and_mirrors_the_first(self):
        """Multi-subject signup. `_provision_faculty` used to read a singular
        `course_application` dict and create exactly ONE row, so a faculty
        member teaching several subjects had to add the rest from
        /form-fillup after approval — a form a PENDING applicant can't reach.
        It also never populated tp.subject, leaving the admin approval card's
        one-line summary blank for every applicant from this form."""
        from accounts.models import TeacherCourseApplication
        res = APIClient().post("/api/accounts/signup/", {
            "email": "multi@test.com", "password": "s3cret-pass!23",
            "role": "TEACHER", "teacher_type": "FACULTY", "terms_accepted": True,
            "faculty_profile": {
                "highest_degree": "masters",
                "course_applications": [
                    {"subject": "physics", "classes": ["11_12"], "streams": ["science"]},
                    {"subject": "mathematics", "classes": ["9_10", "11_12"], "streams": ["science"]},
                    {"subject": "sociology", "classes": ["11_12"], "streams": ["arts"]},
                ],
            },
        }, format="json")
        self.assertIn(res.status_code, (200, 201), res.data)

        tp = TeacherProfile.objects.get(user__email="multi@test.com")
        apps = list(TeacherCourseApplication.objects.filter(teacher_profile=tp))
        self.assertEqual(len(apps), 3)
        self.assertEqual(
            {a.subject for a in apps},
            {"physics", "mathematics", "sociology"},
        )
        maths = next(a for a in apps if a.subject == "mathematics")
        self.assertEqual(sorted(maths.classes), ["11_12", "9_10"])
        # Headline fields mirror the FIRST application.
        self.assertEqual(tp.subject, "physics")
        self.assertEqual(tp.classes, ["11_12"])
        self.assertEqual(tp.streams, ["science"])

    def test_signup_drops_duplicate_and_unknown_subjects(self):
        from accounts.models import TeacherCourseApplication
        res = APIClient().post("/api/accounts/signup/", {
            "email": "dupes@test.com", "password": "s3cret-pass!23",
            "role": "TEACHER", "teacher_type": "FACULTY", "terms_accepted": True,
            "faculty_profile": {
                "course_applications": [
                    {"subject": "physics", "classes": ["11_12"], "streams": ["science"]},
                    {"subject": "physics", "classes": ["9_10"], "streams": ["science"]},
                    {"subject": "not_a_real_subject", "classes": ["9_10"], "streams": ["science"]},
                    {"subject": "", "classes": [], "streams": []},
                    "not even a dict",
                ],
            },
        }, format="json")
        self.assertIn(res.status_code, (200, 201), res.data)
        tp = TeacherProfile.objects.get(user__email="dupes@test.com")
        apps = list(TeacherCourseApplication.objects.filter(teacher_profile=tp))
        self.assertEqual([a.subject for a in apps], ["physics"])

    def test_approval_queue_lists_every_subject_human_labelled(self):
        """The admin review queue showed only the LATEST application's raw
        subject value, so a multi-subject applicant looked like a
        single-subject one to whoever approves them."""
        from accounts.serializers import TeacherTrackApprovalSerializer
        APIClient().post("/api/accounts/signup/", {
            "email": "queue@test.com", "password": "s3cret-pass!23",
            "role": "TEACHER", "teacher_type": "FACULTY", "terms_accepted": True,
            "faculty_profile": {
                "course_applications": [
                    {"subject": "physics", "classes": ["11_12"], "streams": ["science"]},
                    {"subject": "sociology", "classes": ["11_12"], "streams": ["arts"]},
                ],
            },
        }, format="json")
        tp = TeacherProfile.objects.get(user__email="queue@test.com")
        subjects = TeacherTrackApprovalSerializer(tp).data["subjects"]
        # Human labels, every subject, in the order applied.
        self.assertEqual(subjects, "Physics, Sociology")

    def test_signup_still_accepts_the_legacy_single_course_application(self):
        """An older cached JS bundle keeps sending the singular dict; it must
        not silently lose the applicant's only subject mid-deploy."""
        from accounts.models import TeacherCourseApplication
        res = APIClient().post("/api/accounts/signup/", {
            "email": "legacy@test.com", "password": "s3cret-pass!23",
            "role": "TEACHER", "teacher_type": "FACULTY", "terms_accepted": True,
            "faculty_profile": {
                "course_application": {
                    "subject": "chemistry", "classes": ["11_12"], "streams": ["science"],
                },
            },
        }, format="json")
        self.assertIn(res.status_code, (200, 201), res.data)
        tp = TeacherProfile.objects.get(user__email="legacy@test.com")
        apps = list(TeacherCourseApplication.objects.filter(teacher_profile=tp))
        self.assertEqual([a.subject for a in apps], ["chemistry"])
        self.assertEqual(tp.subject, "chemistry")

    def test_teacher_profile_view_saves_file_and_version_together(self):
        # BUG-2: TEACHER_FILE_FIELDS was missing "signed_agreement", so this
        # endpoint bound a version but silently discarded the uploaded file.
        from django.core.files.uploadedfile import SimpleUploadedFile
        from accounts.models import Role, UserRole, LearnerProfile

        self._publish("terms")
        teacher = User.objects.create_user(
            username="t3", email="t3@test.com", password=self.PASSWORD,
        )
        role = Role.objects.create(name="TEACHER")
        UserRole.objects.create(user=teacher, role=role, is_active=True, is_primary=True)
        LearnerProfile.objects.create(account=teacher, display_name="T3", is_default=True)

        c = APIClient()
        c.force_authenticate(user=teacher)
        f = SimpleUploadedFile("signed.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        res = c.patch("/api/accounts/teacher/profile/", {"signed_agreement": f}, format="multipart")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        tp = TeacherProfile.objects.get(user=teacher)
        self.assertTrue(bool(tp.signed_agreement))
        self.assertIsNotNone(tp.signed_agreement_version_id)
        self.assertIsNotNone(tp.signed_agreement_at)
