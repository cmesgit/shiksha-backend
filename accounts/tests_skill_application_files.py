"""
Cover for the attachment on a teacher's skill application
(`TeacherFormFillupSerializer.update`, accounts/serializers.py).

The bug these exist for: the serializer REPLACES `tp.skill_applications`
wholesale on every save. A browser cannot pre-populate a file input, so an
applicant re-saving the form — to fix a typo, or because another section
failed validation — sends no `skill_file_i` for a document they uploaded on
an earlier visit. The replacement row was created empty and the attachment
was gone, while the form had been telling them "File already uploaded".

Nothing caught it because there was no test for this serializer's file
handling at all, and because the destroyed file leaves no trace: the row is
recreated successfully and the request returns 200.

The second thing under test is WHY the carry-over matches on skill name
rather than on list position. `skill_file_i` is indexed against the
submitted list, so deleting a row shifts every later index — matching on
position would re-file one applicant's document under a different skill,
which is worse than losing it.

Run with:
    DJANGO_SETTINGS_MODULE=config.settings_test .venv/bin/python manage.py test \
        accounts.tests_skill_application_files -v2
"""
import json

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from accounts.models import (
    LearnerProfile, TeacherProfile, TeacherSkillApplication, User,
)
from accounts.uploads import discard_superseded_upload
from accounts.serializers import (
    MAX_SKILL_FILE_BYTES, TeacherFormFillupSerializer,
)


class _FakeRequest:
    """The serializer only ever reads `request.FILES`."""

    def __init__(self, files=None):
        self.FILES = files or {}


def _entry(name, subject="mathematics"):
    return {
        "skill_name": name,
        "skill_description": f"Teaching {name}",
        "skill_related_subject": subject,
    }


class SkillApplicationFileCarryOverTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="skillapp", email="skillapp@example.com", password="whatever-9",
        )
        # update() writes the personal section to the default learner profile
        # before it reaches the skill applications.
        LearnerProfile.objects.create(
            account=self.user, display_name="Skill App",
            relationship="SELF", is_default=True,
        )
        self.tp, _ = TeacherProfile.objects.get_or_create(user=self.user)

    def _save(self, entries, files=None):
        # update() reads the uploaded files off the serializer context.
        serializer = TeacherFormFillupSerializer(
            context={"request": _FakeRequest(files)}
        )
        serializer.update(self.user, {"skill_applications": entries})
        return list(self.tp.skill_applications.order_by("id"))

    def _upload(self, i, content=b"a pdf", name="cert.pdf"):
        return {f"skill_file_{i}": SimpleUploadedFile(name, content,
                                                      content_type="application/pdf")}

    # --- the regression ---------------------------------------------------

    def test_resaving_without_reattaching_keeps_the_file(self):
        """The whole point. A second save with no file must not wipe the
        first save's upload."""
        self._save([_entry("Pottery")], self._upload(0))
        stored = self.tp.skill_applications.get().supporting_file.name
        self.assertTrue(stored)

        rows = self._save([_entry("Pottery")])  # no files this time

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].supporting_file.name, stored)

    def test_resaving_still_lets_a_new_file_replace_the_old_one(self):
        """The carry-over must not make the attachment read-only."""
        self._save([_entry("Pottery")], self._upload(0, b"first", "first.pdf"))
        first = self.tp.skill_applications.get().supporting_file.name

        rows = self._save([_entry("Pottery")],
                          self._upload(0, b"second", "second.pdf"))

        self.assertNotEqual(rows[0].supporting_file.name, first)
        self.assertIn("second", rows[0].supporting_file.name)

    def test_editing_another_field_does_not_cost_the_file(self):
        """The realistic path: they came back to reword the description."""
        self._save([_entry("Pottery")], self._upload(0))
        stored = self.tp.skill_applications.get().supporting_file.name

        entry = _entry("Pottery")
        entry["skill_description"] = "Wheel-thrown pottery, ten years"
        rows = self._save([entry])

        self.assertEqual(rows[0].supporting_file.name, stored)
        self.assertEqual(rows[0].skill_description, "Wheel-thrown pottery, ten years")

    # --- why it matches on name, not position -----------------------------

    def test_removing_a_row_does_not_shift_a_file_onto_another_skill(self):
        """Position-matching would hand Pottery's document to Welding here,
        because Welding moves from index 1 to index 0. Attaching the wrong
        applicant document to the wrong skill is worse than losing it."""
        self._save(
            [_entry("Pottery"), _entry("Welding")],
            {**self._upload(0, b"pottery", "pottery.pdf"),
             **self._upload(1, b"welding", "welding.pdf")},
        )
        welding_file = self.tp.skill_applications.get(
            skill_name="Welding").supporting_file.name

        rows = self._save([_entry("Welding")])  # Pottery removed

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].skill_name, "Welding")
        self.assertEqual(rows[0].supporting_file.name, welding_file)
        self.assertNotIn("pottery", rows[0].supporting_file.name)

    def test_the_name_match_ignores_case_and_padding(self):
        self._save([_entry("Pottery")], self._upload(0))
        stored = self.tp.skill_applications.get().supporting_file.name

        rows = self._save([_entry("  pottery ")])

        self.assertEqual(rows[0].supporting_file.name, stored)

    def test_one_stored_file_is_not_cloned_onto_two_same_named_skills(self):
        """Duplicate skill names are possible; each stored file may be
        consumed once, so the second row is genuinely empty rather than
        claiming a document that was never uploaded for it."""
        self._save([_entry("Pottery")], self._upload(0))

        rows = self._save([_entry("Pottery"), _entry("Pottery")])

        with_file = [r for r in rows if r.supporting_file]
        self.assertEqual(len(with_file), 1)

    def test_a_skill_that_never_had_a_file_stays_empty(self):
        rows = self._save([_entry("Pottery")])
        self.assertFalse(rows[0].supporting_file)

    def test_the_carry_over_is_scoped_to_this_teacher(self):
        """A stored name must never be adopted from someone else's row."""
        other = User.objects.create_user(
            username="other", email="other@example.com", password="whatever-9")
        other_tp, _ = TeacherProfile.objects.get_or_create(user=other)
        TeacherSkillApplication.objects.create(
            teacher_profile=other_tp, skill_name="Pottery",
            skill_description="x", skill_related_subject="mathematics",
            supporting_file="teachers/skills/files/someone-else.pdf",
        )

        rows = self._save([_entry("Pottery")])

        self.assertFalse(rows[0].supporting_file)


class SkillApplicationFileSizeTests(TestCase):
    """The form has always said "max 50MB" and nothing enforced it.

    Django does not limit uploads: FILE_UPLOAD_MAX_MEMORY_SIZE is only the
    memory-vs-tempfile threshold and MultiPartParser never applies
    DATA_UPLOAD_MAX_MEMORY_SIZE to FILE fields. Both happen to be set to
    50MB in this project, which is exactly what made the label look real.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="sizer", email="sizer@example.com", password="whatever-9")
        LearnerProfile.objects.create(
            account=self.user, display_name="Sizer",
            relationship="SELF", is_default=True)
        TeacherProfile.objects.get_or_create(user=self.user)

    def _skill_errors(self, size):
        """Return only the `skill_applications` errors.

        The rest of the form is deliberately left incomplete: this asserts on
        the ONE field under test rather than on overall validity, so it can
        never pass or fail because some unrelated required field changed.
        That the check survives an otherwise-invalid form is the point — it
        lives at field level precisely so it reports in the same pass.
        """
        upload = SimpleUploadedFile("big.pdf", b"x" * size,
                                    content_type="application/pdf")
        serializer = TeacherFormFillupSerializer(
            data={"skill_applications": json.dumps([_entry("Pottery")])},
            context={"request": _FakeRequest({"skill_file_0": upload})},
        )
        serializer.is_valid()
        return " ".join(str(e) for e in serializer.errors.get("skill_applications", []))

    def test_a_file_over_the_limit_is_refused(self):
        self.assertIn("limit is 50 MB", self._skill_errors(MAX_SKILL_FILE_BYTES + 1))

    def test_the_message_names_the_real_size_and_the_file(self):
        """"Too large" with no number leaves the applicant guessing how much
        to cut, and which of several files to cut."""
        text = self._skill_errors(MAX_SKILL_FILE_BYTES + 1)
        self.assertIn("50.0 MB", text)
        self.assertIn("big.pdf", text)

    def test_a_file_at_the_limit_is_accepted(self):
        self.assertNotIn("limit is", self._skill_errors(MAX_SKILL_FILE_BYTES))

    def test_a_normal_file_is_accepted(self):
        self.assertNotIn("limit is", self._skill_errors(1024))


class SupersededUploadTests(TestCase):
    """Replacing a file used to leave the old object in the bucket forever.

    That matters more than storage cost: these are KYC scans in a bucket
    that is publicly readable, so a blurred ID photo the applicant retook
    outlived the one that replaced it, indefinitely.

    The reference guard is the load-bearing part. Media has no backup — the
    bucket is the only copy — and dev and prod share one Edge Storage zone,
    so deleting a name any row still points at is unrecoverable.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="sup", email="sup@example.com", password="whatever-9")
        LearnerProfile.objects.create(
            account=self.user, display_name="Sup",
            relationship="SELF", is_default=True)
        self.tp, _ = TeacherProfile.objects.get_or_create(user=self.user)

    def _store(self, name, body=b"bytes"):
        return default_storage.save(name, ContentFile(body))

    def test_an_unreferenced_file_is_deleted(self):
        stored = self._store("teachers/skills/files/orphan.pdf")
        self.assertTrue(default_storage.exists(stored))

        self.assertTrue(discard_superseded_upload(stored))
        self.assertFalse(default_storage.exists(stored))

    def test_a_file_another_row_still_points_at_is_kept(self):
        """The guard. Without it, a shared object is deleted out from under
        whoever still references it — and there is no backup to restore."""
        stored = self._store("teachers/skills/files/shared.pdf")
        TeacherSkillApplication.objects.create(
            teacher_profile=self.tp, skill_name="Pottery",
            skill_description="d", skill_related_subject="mathematics",
            supporting_file=stored,
        )

        self.assertFalse(discard_superseded_upload(stored))
        self.assertTrue(default_storage.exists(stored))

    def test_a_file_referenced_by_a_kyc_field_is_kept(self):
        """The guard must span every model that can hold a name, not just
        the one being edited."""
        stored = self._store("teachers/id_proofs/front.jpg")
        self.tp.id_proof_front = stored
        self.tp.save(update_fields=["id_proof_front"])

        self.assertFalse(discard_superseded_upload(stored))
        self.assertTrue(default_storage.exists(stored))

    def test_an_empty_name_is_a_no_op(self):
        self.assertFalse(discard_superseded_upload(""))
        self.assertFalse(discard_superseded_upload(None))

    def test_a_missing_object_does_not_raise(self):
        """Housekeeping must never cost the applicant their submission."""
        self.assertFalse(
            discard_superseded_upload("teachers/skills/files/never-existed.pdf"))

    def test_removing_a_skill_discards_its_file(self):
        """End to end through the serializer: drop the skill, lose the file."""
        serializer = TeacherFormFillupSerializer(
            context={"request": _FakeRequest(
                {"skill_file_0": SimpleUploadedFile("gone.pdf", b"x",
                                                    content_type="application/pdf")})})
        serializer.update(self.user, {"skill_applications": [_entry("Pottery")]})
        stored = self.tp.skill_applications.get().supporting_file.name
        self.assertTrue(default_storage.exists(stored))

        # Re-save with the skill removed entirely.
        TeacherFormFillupSerializer(
            context={"request": _FakeRequest()}
        ).update(self.user, {"skill_applications": []})

        self.assertEqual(self.tp.skill_applications.count(), 0)
        self.assertFalse(default_storage.exists(stored))

    def test_a_carried_over_file_is_NOT_discarded(self):
        """The dangerous interaction: the carry-over re-points a new row at
        the old name, so the cleanup must see the final state and leave it
        alone. Getting this wrong deletes the file it just preserved."""
        serializer = TeacherFormFillupSerializer(
            context={"request": _FakeRequest(
                {"skill_file_0": SimpleUploadedFile("keep.pdf", b"x",
                                                    content_type="application/pdf")})})
        serializer.update(self.user, {"skill_applications": [_entry("Pottery")]})
        stored = self.tp.skill_applications.get().supporting_file.name

        TeacherFormFillupSerializer(
            context={"request": _FakeRequest()}
        ).update(self.user, {"skill_applications": [_entry("Pottery")]})

        self.assertEqual(self.tp.skill_applications.get().supporting_file.name, stored)
        self.assertTrue(default_storage.exists(stored))


class KycFileSizeTests(TestCase):
    """The KYC documents had the same non-limit as the skill attachment."""

    FIELDS = ["qualification_certificate", "id_proof_front",
              "id_proof_back", "signed_agreement"]

    def _errors_for(self, field, size):
        upload = SimpleUploadedFile("scan.jpg", b"x" * size,
                                    content_type="image/jpeg")
        serializer = TeacherFormFillupSerializer(data={field: upload})
        serializer.is_valid()
        return " ".join(str(e) for e in serializer.errors.get(field, []))

    def test_every_document_field_rejects_an_oversized_file(self):
        for field in self.FIELDS:
            with self.subTest(field=field):
                self.assertIn(
                    "limit is 50 MB",
                    self._errors_for(field, MAX_SKILL_FILE_BYTES + 1))

    def test_every_document_field_accepts_a_normal_file(self):
        for field in self.FIELDS:
            with self.subTest(field=field):
                self.assertNotIn("limit is", self._errors_for(field, 1024))
