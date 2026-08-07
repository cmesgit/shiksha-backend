"""
scholarship/tests.py — the properties that actually matter for this module:
the dedup constraint really blocks a second attempt (and really allows a
sibling), the server deadline really rejects a late write, the student-facing
serializer really never leaks the answer key, and scoring really produces the
right band + a locked award while the platform is free.
"""
import io
import zipfile
from datetime import date, datetime, timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status as http_status

from accounts.models import LearnerProfile, Role, User, UserRole
from courses.models import Course
from global_settings.models import GlobalSettings

from . import aadhaar_offline, services
from .models import (
    ExamSession,
    GuardianVerification,
    ScholarshipAward,
    ScholarshipBand,
    ScholarshipEligibilityRecord,
    ScholarshipQuestionBankItem,
    ScholarshipSettings,
)
from .serializers import ExamQuestionStudentSerializer

SUBJECTS = [c[0] for c in ScholarshipQuestionBankItem.SUBJECT_CHOICES]


def make_bank(class_level=10, per_bucket=10):
    """Enough questions in every (subject, difficulty) cell to fill a
    50-question paper at the default 60/30/10 split without shortages."""
    items = []
    for subject in SUBJECTS:
        for difficulty in (
            ScholarshipQuestionBankItem.DIFFICULTY_EASY,
            ScholarshipQuestionBankItem.DIFFICULTY_MEDIUM,
            ScholarshipQuestionBankItem.DIFFICULTY_HARD,
        ):
            for i in range(per_bucket):
                items.append(ScholarshipQuestionBankItem(
                    class_level=class_level, subject=subject, difficulty=difficulty,
                    text=f"{subject}/{difficulty} Q{i}",
                    options=["A", "B", "C", "D"], correct_option_index=0, is_active=True,
                ))
    ScholarshipQuestionBankItem.objects.bulk_create(items)


def make_bands():
    ScholarshipBand.objects.bulk_create([
        ScholarshipBand(min_correct=50, max_correct=50, discount_pct=50),
        ScholarshipBand(min_correct=45, max_correct=49, discount_pct=40),
        ScholarshipBand(min_correct=40, max_correct=44, discount_pct=35),
        ScholarshipBand(min_correct=35, max_correct=39, discount_pct=30),
        ScholarshipBand(min_correct=30, max_correct=34, discount_pct=20),
        ScholarshipBand(min_correct=25, max_correct=29, discount_pct=10),
        ScholarshipBand(min_correct=0, max_correct=24, discount_pct=0),
    ])


class EligibilityDedupTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")
        cls.parent = User.objects.create_user(username="parent1", email="parent1@test.com", password="x")
        cls.guardian = GuardianVerification.objects.create(
            account=cls.parent, method=GuardianVerification.METHOD_MANUAL,
            status=GuardianVerification.STATUS_VERIFIED, provider_reference="",
        )
        cls.child_a = LearnerProfile.objects.create(
            account=cls.parent, display_name="A", full_name="Kid A",
            date_of_birth=date(2011, 1, 1), current_class="10", academic_year="2026-27",
        )
        cls.child_b = LearnerProfile.objects.create(
            account=cls.parent, display_name="B", full_name="Kid B",
            date_of_birth=date(2013, 1, 1), current_class="8", academic_year="2026-27",
        )

    def test_second_attempt_same_child_same_year_blocked(self):
        services.get_or_reserve_eligibility(
            learner_profile=self.child_a, guardian_verification=self.guardian, academic_year="2026-27",
        )
        record2 = services.get_or_reserve_eligibility(
            learner_profile=self.child_a, guardian_verification=self.guardian, academic_year="2026-27",
        )
        # Idempotent while still reserved — same row, not a second one.
        record1 = ScholarshipEligibilityRecord.objects.get(learner_profile=self.child_a)
        self.assertEqual(record1.id, record2.id)

        record1.status = ScholarshipEligibilityRecord.STATUS_CONSUMED
        record1.save()
        with self.assertRaises(services.AlreadyAttemptedError):
            services.get_or_reserve_eligibility(
                learner_profile=self.child_a, guardian_verification=self.guardian, academic_year="2026-27",
            )

    def test_sibling_gets_own_attempt(self):
        rec_a = services.get_or_reserve_eligibility(
            learner_profile=self.child_a, guardian_verification=self.guardian, academic_year="2026-27",
        )
        rec_b = services.get_or_reserve_eligibility(
            learner_profile=self.child_b, guardian_verification=self.guardian, academic_year="2026-27",
        )
        self.assertNotEqual(rec_a.dedup_hash, rec_b.dedup_hash)

    def test_same_child_new_academic_year_gets_new_attempt(self):
        rec1 = services.get_or_reserve_eligibility(
            learner_profile=self.child_a, guardian_verification=self.guardian, academic_year="2026-27",
        )
        rec1.status = ScholarshipEligibilityRecord.STATUS_CONSUMED
        rec1.save()
        rec2 = services.get_or_reserve_eligibility(
            learner_profile=self.child_a, guardian_verification=self.guardian, academic_year="2027-28",
        )
        self.assertNotEqual(rec1.id, rec2.id)

    def test_voided_record_frees_the_slot(self):
        rec1 = services.get_or_reserve_eligibility(
            learner_profile=self.child_a, guardian_verification=self.guardian, academic_year="2026-27",
        )
        rec1.status = ScholarshipEligibilityRecord.STATUS_VOIDED
        rec1.save()
        rec2 = services.get_or_reserve_eligibility(
            learner_profile=self.child_a, guardian_verification=self.guardian, academic_year="2026-27",
        )
        self.assertNotEqual(rec1.id, rec2.id)


class BandLookupTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_bands()

    def test_boundaries(self):
        self.assertEqual(services.band_for_score(0).discount_pct, 0)
        self.assertEqual(services.band_for_score(24).discount_pct, 0)
        self.assertEqual(services.band_for_score(25).discount_pct, 10)
        self.assertEqual(services.band_for_score(29).discount_pct, 10)
        self.assertEqual(services.band_for_score(30).discount_pct, 20)
        self.assertEqual(services.band_for_score(50).discount_pct, 50)


class ExamFlowTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")
        make_bank(class_level=10)
        make_bands()
        cls.parent = User.objects.create_user(username="parent2", email="parent2@test.com", password="x")
        cls.guardian = GuardianVerification.objects.create(
            account=cls.parent, method=GuardianVerification.METHOD_MANUAL,
            status=GuardianVerification.STATUS_VERIFIED,
        )
        cls.child = LearnerProfile.objects.create(
            account=cls.parent, display_name="C", full_name="Kid C",
            date_of_birth=date(2011, 1, 1), current_class="10", academic_year="2026-27",
        )
        cls.course = Course.objects.create(title="Class 10 Board Prep", class_level=10, price=1499900)

    def _new_session(self):
        record = services.get_or_reserve_eligibility(
            learner_profile=self.child, guardian_verification=self.guardian, academic_year="2026-27",
        )
        session, _created = services.start_or_resume_exam_session(record, self.course)
        return session

    def test_generates_full_paper_with_no_shortage(self):
        session = self._new_session()
        self.assertEqual(session.questions.count(), ScholarshipSettings.load().question_count)

    def test_student_serializer_never_leaks_answer_key(self):
        session = self._new_session()
        data = ExamQuestionStudentSerializer(session.questions.all(), many=True).data
        for row in data:
            self.assertNotIn("correct_option_index", row)

    def test_late_answer_rejected_by_server_deadline(self):
        session = self._new_session()
        session.deadline = timezone.now() - timezone.timedelta(seconds=1)
        session.save(update_fields=["deadline"])
        question = session.questions.first()
        with self.assertRaises(services.DeadlinePassedError):
            services.record_answer(session, question, selected_option_index=0)

    def test_expire_if_past_deadline_auto_submits_and_scores(self):
        session = self._new_session()
        session.deadline = timezone.now() - timezone.timedelta(seconds=1)
        session.save(update_fields=["deadline"])
        session = services.expire_if_past_deadline(session)
        self.assertEqual(session.status, ExamSession.STATUS_EXPIRED)
        self.assertIsNotNone(session.score)

    def test_submit_all_correct_awards_top_band_and_locks_while_free(self):
        GlobalSettings.load()  # free_trial_enabled defaults True
        session = self._new_session()
        for q in session.questions.all():
            services.record_answer(session, q, selected_option_index=q.correct_option_index)
        session = services.submit_exam(session)
        self.assertEqual(session.score, ScholarshipSettings.load().question_count)
        self.assertEqual(session.awarded_discount_pct, 50)

        award = ScholarshipAward.objects.get(exam_session=session)
        self.assertEqual(award.discount_pct, 50)
        self.assertEqual(award.status, ScholarshipAward.STATUS_LOCKED)
        self.assertEqual(award.course_id, self.course.id)

    def test_submit_all_wrong_gives_no_award(self):
        session = self._new_session()
        for q in session.questions.all():
            wrong = (q.correct_option_index + 1) % 4
            services.record_answer(session, q, selected_option_index=wrong)
        session = services.submit_exam(session)
        self.assertEqual(session.score, 0)
        self.assertFalse(ScholarshipAward.objects.filter(exam_session=session).exists())

    def test_submit_is_idempotent(self):
        session = self._new_session()
        services.submit_exam(session)
        award_count_before = ScholarshipAward.objects.count()
        services.submit_exam(session)  # second call must not re-score or double-mint
        self.assertEqual(ScholarshipAward.objects.count(), award_count_before)


class FreeEnrollRedemptionTest(TestCase):
    """The additive integration point in enrollments/payment_views.py must
    mark an earned award redeemed on successful free-enroll, without
    breaking free-enroll for a course with no award at all."""

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")
        make_bank(class_level=10)
        make_bands()
        cls.parent = User.objects.create_user(
            username="parent3", email="parent3@test.com", password="x", is_verified=True,
        )
        cls.guardian = GuardianVerification.objects.create(
            account=cls.parent, method=GuardianVerification.METHOD_MANUAL,
            status=GuardianVerification.STATUS_VERIFIED,
        )
        cls.child = LearnerProfile.objects.create(
            account=cls.parent, display_name="D", full_name="Kid D",
            date_of_birth=date(2011, 1, 1), current_class="10", academic_year="2026-27", is_default=True,
        )
        cls.course = Course.objects.create(title="Class 10 Board Prep", class_level=10, price=1499900,
                                            status="PUBLISHED")

    def client_as_child(self):
        c = APIClient()
        c.force_authenticate(
            user=self.parent, token={"context": "learner", "active_profile": str(self.child.id)},
        )
        return c

    def test_free_enroll_without_award_still_works(self):
        res = self.client_as_child().post("/api/enrollments/free-enroll/", {"course": str(self.course.id)})
        self.assertEqual(res.status_code, 201)

    def test_current_session_endpoint_supports_resume_banner(self):
        c = self.client_as_child()
        res = c.get("/api/scholarship/exam/session/current/")
        self.assertEqual(res.status_code, http_status.HTTP_404_NOT_FOUND)

        record = services.get_or_reserve_eligibility(
            learner_profile=self.child, guardian_verification=self.guardian, academic_year="2026-27",
        )
        session, _ = services.start_or_resume_exam_session(record, self.course)
        res = c.get("/api/scholarship/exam/session/current/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["id"], str(session.id))

        services.submit_exam(session)
        res = c.get("/api/scholarship/exam/session/current/")
        self.assertEqual(res.status_code, http_status.HTTP_404_NOT_FOUND)

    def test_free_enroll_redeems_earned_award(self):
        record = services.get_or_reserve_eligibility(
            learner_profile=self.child, guardian_verification=self.guardian, academic_year="2026-27",
        )
        session, _ = services.start_or_resume_exam_session(record, self.course)
        for q in session.questions.all():
            services.record_answer(session, q, selected_option_index=q.correct_option_index)
        services.submit_exam(session)
        award = ScholarshipAward.objects.get(exam_session=session)
        self.assertEqual(award.status, ScholarshipAward.STATUS_LOCKED)

        res = self.client_as_child().post("/api/enrollments/free-enroll/", {"course": str(self.course.id)})
        self.assertEqual(res.status_code, 201)
        award.refresh_from_db()
        self.assertEqual(award.status, ScholarshipAward.STATUS_REDEEMED)
        self.assertIsNotNone(award.redeemed_at)


def _build_ekyc_xml(reference_id="925020190122165455195", name="Test Guardian", dob="15-06-1985", gender="male"):
    return (
        f'<OfflinePaperlessKyc referenceId="{reference_id}">'
        f'<UidData><Poi dob="{dob}" gender="{gender}" name="{name}"/></UidData>'
        f"</OfflinePaperlessKyc>"
    ).encode("utf-8")


def _sign_with_throwaway_key(xml_bytes):
    """A syntactically-correct, INTERNALLY CONSISTENT signature — but from a
    throwaway key, never UIDAI's. This is the fixture that matters most:
    real UIDAI-signed test data can't be obtained without a real Aadhaar
    holder's OTP, so the one thing we CAN and MUST prove is that
    verify_offline_ekyc rejects anything not signed by the pinned UIDAI
    key — i.e. that it never fails open."""
    from lxml import etree
    from signxml import XMLSigner
    from cryptography.hazmat.primitives.asymmetric import rsa

    root = etree.fromstring(xml_bytes)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signed_root = XMLSigner().sign(root, key=key, cert=None)
    return etree.tostring(signed_root)


def _zip_bytes(xml_bytes, share_code=None):
    """A plain (unencrypted) zip — fine for fixtures where encryption isn't
    what's under test (malformed-zip, wrong-root-element, staleness)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("offline_ekyc.xml", xml_bytes)
    return buf.getvalue()


def _encrypted_zip_bytes(xml_bytes, share_code):
    """A GENUINELY password-encrypted zip, via the system `zip` binary.

    Python's stdlib `zipfile` can only READ ZipCrypto-encrypted entries, not
    write them — `ZipFile.setpassword()` before `writestr()` is silently a
    no-op (verified directly: reading such a "protected" file back with the
    WRONG password succeeds, since nothing was ever encrypted). Shelling out
    to the real `zip` CLI is also more representative of production: a real
    user's Aadhaar Offline e-KYC download is itself produced by ordinary
    consumer zip tooling, not a Python script."""
    import shutil
    import subprocess
    import tempfile

    if shutil.which("zip") is None:
        return None  # caller should skip; see AadhaarOfflineModuleTest setup

    with tempfile.TemporaryDirectory() as tmp:
        xml_path = f"{tmp}/offline_ekyc.xml"
        zip_path = f"{tmp}/ekyc.zip"
        with open(xml_path, "wb") as f:
            f.write(xml_bytes)
        subprocess.run(
            ["zip", "-j", "-P", share_code, zip_path, xml_path],
            check=True, capture_output=True,
        )
        with open(zip_path, "rb") as f:
            return f.read()


class AadhaarOfflineModuleTest(TestCase):
    """Unit tests on the pure verification functions — no DB, no HTTP.

    IMPORTANT LIMITATION, read before trusting this test file too much: a
    genuine UIDAI-signed Offline e-KYC document can only be obtained from a
    real Aadhaar holder completing UIDAI's own OTP flow — there is no way to
    fabricate one, and asking for real Aadhaar data to build a test fixture
    would itself be a bad idea. These tests therefore prove the SAFE
    direction (forged/invalid input is correctly rejected, freshness and
    parsing edge cases are handled) but cannot prove the success path
    against real UIDAI-signed data. Do that manually with a real document
    before relying on this in production — see aadhaar_offline.py's
    module docstring.
    """

    def test_rejects_signature_not_from_uidai(self):
        fresh_ref = "9250" + timezone.now().strftime("%Y%m%d%H%M%S") + "000"
        xml = _build_ekyc_xml(reference_id=fresh_ref)
        forged = _sign_with_throwaway_key(xml)
        zip_bytes = _zip_bytes(forged)

        with self.assertRaises(aadhaar_offline.AadhaarOfflineVerificationError):
            aadhaar_offline.verify_offline_ekyc(zip_bytes, "TEST")

    def test_rejects_wrong_share_code(self):
        xml = _build_ekyc_xml()
        zip_bytes = _encrypted_zip_bytes(xml, "RIGHT")
        if zip_bytes is None:
            self.skipTest("system `zip` binary not available to build an encrypted test fixture")
        with self.assertRaises(aadhaar_offline.AadhaarOfflineVerificationError) as ctx:
            aadhaar_offline.verify_offline_ekyc(zip_bytes, "WRONG")
        self.assertIn("share code", str(ctx.exception).lower())

    def test_accepts_correct_share_code_then_fails_on_signature(self):
        """Confirms the encrypted-zip fixture itself is sound (the RIGHT
        password does open it) — isolates the share-code check from the
        signature check, which the forged-signature test above already
        covers on its own with a plain zip. Needs a fresh reference_id so
        it actually reaches signature verification rather than tripping
        the (unrelated) freshness check first."""
        fresh_ref = "9250" + timezone.now().strftime("%Y%m%d%H%M%S") + "000"
        xml = _build_ekyc_xml(reference_id=fresh_ref)
        zip_bytes = _encrypted_zip_bytes(xml, "RIGHT")
        if zip_bytes is None:
            self.skipTest("system `zip` binary not available to build an encrypted test fixture")
        # Right password opens the zip fine; the *unsigned* XML inside then
        # fails at signature verification, not at password-checking — proves
        # the two failure modes are properly distinguished.
        with self.assertRaises(aadhaar_offline.AadhaarOfflineVerificationError) as ctx:
            aadhaar_offline.verify_offline_ekyc(zip_bytes, "RIGHT")
        self.assertIn("signature", str(ctx.exception).lower())

    def test_rejects_malformed_zip(self):
        with self.assertRaises(aadhaar_offline.AadhaarOfflineVerificationError):
            aadhaar_offline.verify_offline_ekyc(b"not a zip file", "TEST")

    def test_rejects_non_ekyc_xml_root(self):
        xml = b"<SomethingElse/>"
        zip_bytes = _zip_bytes(xml)
        with self.assertRaises(aadhaar_offline.AadhaarOfflineVerificationError):
            aadhaar_offline.verify_offline_ekyc(zip_bytes, "TEST")

    def test_rejects_stale_document(self):
        old_ref = "9250" + "20180101120000" + "000"  # 2018 — long past MAX_DOCUMENT_AGE_DAYS
        xml = _build_ekyc_xml(reference_id=old_ref)
        signed = _sign_with_throwaway_key(xml)
        zip_bytes = _zip_bytes(signed)
        with self.assertRaises(aadhaar_offline.AadhaarOfflineVerificationError) as ctx:
            aadhaar_offline.verify_offline_ekyc(zip_bytes, "TEST")
        self.assertIn("days old", str(ctx.exception))

    def test_dedup_reference_deterministic_and_excludes_reference_id(self):
        fields_a = {"name": "Same Person", "dob": "01-01-1980", "gender": "male", "reference_id": "111111111111111111111"}
        fields_b = {"name": "Same Person", "dob": "01-01-1980", "gender": "male", "reference_id": "999999999999999999999"}
        # Different reference_id (different Aadhaar-derived digits + timestamp)
        # but same verified demographics -> same dedup reference. Proves the
        # hash genuinely excludes the Aadhaar-derived field.
        self.assertEqual(
            aadhaar_offline.dedup_reference_for(fields_a), aadhaar_offline.dedup_reference_for(fields_b)
        )

    def test_dedup_reference_differs_for_different_people(self):
        fields_a = {"name": "Person A", "dob": "01-01-1980", "gender": "male", "reference_id": "1"}
        fields_b = {"name": "Person B", "dob": "02-02-1990", "gender": "female", "reference_id": "1"}
        self.assertNotEqual(
            aadhaar_offline.dedup_reference_for(fields_a), aadhaar_offline.dedup_reference_for(fields_b)
        )

    def test_dedup_reference_never_contains_reference_id(self):
        fields = {"name": "Someone", "dob": "01-01-1980", "gender": "male", "reference_id": "925099999999999999999"}
        result = aadhaar_offline.dedup_reference_for(fields)
        self.assertNotIn("9250", result)
        self.assertNotIn("925099999999999999999", result)


class AadhaarOfflineViewTest(TestCase):
    """Exercises the real endpoint (not just the module) — confirms a
    rejected verification never creates a stray GuardianVerification row,
    and that missing fields are validated before any parsing is attempted."""

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="STUDENT")
        cls.parent = User.objects.create_user(
            username="offlineparent", email="offlineparent@test.com", password="x", is_verified=True,
        )

    def client_as_parent(self):
        c = APIClient()
        c.force_authenticate(user=self.parent, token={"context": "account"})
        return c

    def test_forged_document_rejected_with_no_record_created(self):
        fresh_ref = "9250" + timezone.now().strftime("%Y%m%d%H%M%S") + "000"
        xml = _build_ekyc_xml(reference_id=fresh_ref)
        forged = _sign_with_throwaway_key(xml)
        zip_bytes = _zip_bytes(forged)

        from django.core.files.uploadedfile import SimpleUploadedFile
        res = self.client_as_parent().post(
            "/api/scholarship/verification/",
            {
                "method": "aadhaar_offline",
                "ekyc_zip": SimpleUploadedFile("ekyc.zip", zip_bytes, content_type="application/zip"),
                "share_code": "TEST",
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(GuardianVerification.objects.filter(account=self.parent).exists())

    def test_missing_share_code_rejected_before_parsing(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        res = self.client_as_parent().post(
            "/api/scholarship/verification/",
            {
                "method": "aadhaar_offline",
                "ekyc_zip": SimpleUploadedFile("ekyc.zip", b"irrelevant", content_type="application/zip"),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(GuardianVerification.objects.filter(account=self.parent).exists())

    def test_disabled_when_setting_off(self):
        settings_obj = ScholarshipSettings.load()
        settings_obj.allow_aadhaar_offline = False
        settings_obj.save()
        res = self.client_as_parent().post("/api/scholarship/verification/", {"method": "aadhaar_offline"})
        self.assertEqual(res.status_code, 400)
