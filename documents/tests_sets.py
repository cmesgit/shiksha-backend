"""Phase 1 of design_handoff_explore_verification — sets, files and tags.

The redesign's unit is a *set*: several files under one title, carrying several
`field › subject › branch` tag paths. `Document` became that set rather than
being wrapped by a new one, and grew two children — `DocumentFile` and
`DocumentTopic`.

What is worth testing here is not that the models exist. It is the three places
this change can go wrong quietly:

  * the backfill leaving live documents with no file or no tag, which does not
    error — it just makes them render empty and drop out of the browse facets;
  * the duplicate-upload constraint being lost in the move from Document to
    DocumentFile, which re-enables something the library used to refuse;
  * the field map drifting out of step with SUBJECT_TOPICS, which silently
    files a subject under "Other" and takes it out of its own field row.
"""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase

from .constants import SUBJECT_TOPICS
from .fields import (
    FIELD_ORDER,
    SUBJECT_FIELDS,
    UNFILED,
    field_for_subject,
    field_groups,
)
from .models import Document, DocumentFile, DocumentTopic


def _user(name):
    return get_user_model().objects.create_user(
        username=name, email=f"{name}@example.com", password="pw12345!",
    )


class FieldMapTest(TestCase):
    def test_every_static_subject_has_a_field(self):
        """⚠ THE POINT. `field_for_subject` never raises — an unmapped subject
        silently becomes "Other". That is right for a subject an uploader
        invented, and wrong for one shipped in SUBJECT_TOPICS, which should
        appear under its own heading. Without this test, adding a subject to
        constants.py and forgetting fields.py looks like it worked."""
        unmapped = [s for s in SUBJECT_TOPICS if s not in SUBJECT_FIELDS]
        self.assertEqual(unmapped, [], f"subjects with no field: {unmapped}")

    def test_the_map_invents_no_subjects(self):
        """The other direction. A typo'd key here is dead weight that never
        matches anything, and it would not show up in the test above."""
        unknown = [s for s in SUBJECT_FIELDS if s not in SUBJECT_TOPICS]
        self.assertEqual(unknown, [], f"fields for unknown subjects: {unknown}")

    def test_every_mapped_field_is_in_the_browse_order(self):
        """A field missing from FIELD_ORDER still groups documents but never
        renders a chip, so its subjects become unreachable by browsing."""
        missing = sorted(set(SUBJECT_FIELDS.values()) - set(FIELD_ORDER))
        self.assertEqual(missing, [])

    def test_the_two_placements_the_design_fixed(self):
        """Not arbitrary: the design's browse panel listed Computer Science
        under "SUBJECT IN ENGINEERING", and its sample tag path read
        "Social Sciences › Sociology › Rural sociology". Pinned so a later
        tidy-up of the map cannot quietly contradict the design."""
        self.assertEqual(field_for_subject("Computer Science"), "Engineering")
        self.assertEqual(field_for_subject("Sociology"), "Social Sciences")

    def test_lookup_ignores_case_and_padding(self):
        """`Document.subject` is free text and always has been. "  biology "
        and "Biology" are one subject everywhere else in Explore; if they were
        two here, the field row would count the same documents twice."""
        self.assertEqual(field_for_subject("  biology "), "Sciences")
        self.assertEqual(field_for_subject("BIOLOGY"), "Sciences")

    def test_an_invented_subject_is_still_browsable(self):
        """Uploaders may invent a subject with no approval. Returning "" or
        None here would drop their upload out of the field row entirely —
        publishing it and hiding it at the same time."""
        self.assertEqual(field_for_subject("Underwater Basket Weaving"), UNFILED)
        self.assertIn(UNFILED, FIELD_ORDER)


class FieldGroupsTest(TestCase):
    def test_live_rows_fold_into_the_static_tree(self):
        """A live row spelled differently must extend the canonical subject,
        not appear beside it. Two "Biology" chips differing only in case is
        the exact bug `taxonomy.subject_groups()` already guards against one
        level down."""
        tree = field_groups([("Sciences", "biology", "Genetics")])
        sciences = next(f for f in tree if f["field"] == "Sciences")
        names = [s["subject"] for s in sciences["subjects"]]
        self.assertEqual(names.count("Biology"), 1)
        self.assertNotIn("biology", names)
        biology = next(s for s in sciences["subjects"] if s["subject"] == "Biology")
        self.assertIn("Genetics", biology["branches"])

    def test_a_row_cannot_move_a_known_subject_into_another_field(self):
        """`field` is stored per tag, so a bad or stale value can arrive. The
        canonical placement wins — otherwise one mis-saved row relocates a
        whole subject in everyone's browse panel."""
        tree = field_groups([("Commerce", "Civil Engineering", "Bridges")])
        commerce = next(f for f in tree if f["field"] == "Commerce")
        self.assertNotIn(
            "Civil Engineering", [s["subject"] for s in commerce["subjects"]])
        engineering = next(f for f in tree if f["field"] == "Engineering")
        civil = next(s for s in engineering["subjects"]
                     if s["subject"] == "Civil Engineering")
        self.assertIn("Bridges", civil["branches"])

    def test_empty_fields_still_render(self):
        """The design shows a count on every chip including zero. A field that
        disappears when its last document is removed makes the browse panel
        change shape between page loads."""
        tree = field_groups([])
        self.assertEqual([f["field"] for f in tree][:len(FIELD_ORDER)], FIELD_ORDER)

    def test_the_all_sentinel_is_ignored(self):
        """"all" is the filter's no-op value, not a subject. It reaches this
        function from the same query string the facets do."""
        tree = field_groups([("", "all", "")])
        for field in tree:
            self.assertNotIn("all", [s["subject"] for s in field["subjects"]])


class DocumentFileConstraintTest(TestCase):
    """The duplicate-upload rule after it moved from Document to DocumentFile.

    `Document` carries `documents_unique_owner_file_hash`, which is what stops
    one person uploading the same file twice while letting two different people
    each upload the same public paper. Once a set holds several files that rule
    has to live on the file, and a move like that is exactly the kind of thing
    that gets dropped with nobody noticing until someone's library fills with
    duplicates.
    """

    def setUp(self):
        self.alice = _user("setalice")
        self.bob = _user("setbob")

    def _set(self, owner, title="A set"):
        return Document.objects.create(owner=owner, title=title)

    def _file(self, doc, file_hash, name="a.pdf"):
        return DocumentFile.objects.create(
            document=doc,
            file=SimpleUploadedFile(name, b"x"),
            original_name=name,
            file_hash=file_hash,
        )

    def test_the_same_person_cannot_add_the_same_file_twice(self):
        doc = self._set(self.alice)
        self._file(doc, "abc123")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._file(self._set(self.alice, "Another set"), "abc123")

    def test_two_people_may_each_upload_the_same_file(self):
        """Legitimate, and what the moderation panel's Duplicate Review queue
        is for. A constraint on the hash alone would forbid it."""
        self._file(self._set(self.alice), "shared")
        self._file(self._set(self.bob), "shared")
        self.assertEqual(
            DocumentFile.objects.filter(file_hash="shared").count(), 2)

    def test_blank_hashes_do_not_collide(self):
        """Pre-existing rows and files whose hash was never computed all carry
        "". If those collided, one legacy document would block every other."""
        doc = self._set(self.alice)
        self._file(doc, "", name="a.pdf")
        self._file(doc, "", name="b.pdf")
        self.assertEqual(doc.files.count(), 2)

    def test_removing_a_set_frees_its_owner_to_re_upload(self):
        """`is_removed` is denormalised onto the file for this. If it went
        stale, a soft-removed set would block its owner from re-uploading the
        same file for good — and the error would point at the wrong thing."""
        doc = self._set(self.alice)
        self._file(doc, "dup")
        doc.is_removed = True
        doc.save(update_fields=["is_removed"])
        DocumentFile.objects.filter(document=doc).update(is_removed=True)
        self._file(self._set(self.alice, "Replacement"), "dup")
        self.assertEqual(
            DocumentFile.objects.filter(file_hash="dup", is_removed=False).count(), 1)

    def test_save_keeps_the_denormalised_owner_in_step(self):
        """It is set from the parent in save(), never passed in. A caller that
        supplied the wrong owner would create a row the constraint checks
        against the wrong person."""
        doc = self._set(self.bob)
        f = DocumentFile(
            document=doc, owner=self.alice,
            file=SimpleUploadedFile("c.pdf", b"x"), file_hash="h",
        )
        f.save()
        f.refresh_from_db()
        self.assertEqual(f.owner_id, self.bob.id)


class DocumentTopicTest(TestCase):
    def test_a_set_carries_several_tag_paths(self):
        """The whole reason this is a table. The design's sample set is tagged
        under both Civil Engineering and Sociology, and EITHER tag makes a
        specialist eligible to review it."""
        doc = Document.objects.create(owner=_user("topicowner"), title="Dams")
        DocumentTopic.objects.create(
            document=doc, field="Engineering",
            subject="Civil Engineering", branch="Water resources", order=0)
        DocumentTopic.objects.create(
            document=doc, field="Social Sciences",
            subject="Sociology", branch="Rural sociology", order=1)
        self.assertEqual(doc.topics.count(), 2)
        self.assertEqual(
            [str(t) for t in doc.topics.all()],
            ["Engineering › Civil Engineering › Water resources",
             "Social Sciences › Sociology › Rural sociology"],
        )

    def test_the_same_path_cannot_be_added_twice(self):
        doc = Document.objects.create(owner=_user("dupowner"), title="Dams")
        DocumentTopic.objects.create(
            document=doc, field="Sciences", subject="Physics", branch="Optics")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DocumentTopic.objects.create(
                    document=doc, field="Sciences",
                    subject="Physics", branch="Optics")

    def test_the_same_subject_may_appear_under_two_branches(self):
        doc = Document.objects.create(owner=_user("twobranch"), title="Dams")
        DocumentTopic.objects.create(
            document=doc, subject="Physics", branch="Optics")
        DocumentTopic.objects.create(
            document=doc, subject="Physics", branch="Mechanics")
        self.assertEqual(doc.topics.count(), 2)


class BackfillTest(TestCase):
    """0011, run against documents in the shape they have on prod today.

    A backfill that misses rows does not fail — it leaves those documents with
    no files and no tags, which renders as an empty set and drops them out of
    the browse facets. That is why this exercises the migration's own function
    rather than asserting the end state of a fresh database, where every row
    was created by the new code path anyway.
    """

    def setUp(self):
        from importlib import import_module
        self.mod = import_module(
            "documents.migrations.0011_backfill_set_files_and_topics")
        self.owner = _user("backfillowner")

    def _run(self):
        from django.apps import apps
        self.mod.forwards(apps, None)

    def _legacy(self, **kw):
        """A Document as it exists before this phase: one file on the parent,
        a subject and a topic, no children."""
        defaults = dict(
            owner=self.owner, title="Legacy doc",
            subject="Civil Engineering", topic="Structural",
            filetype="PDF", pages=12, file_hash="",
            file=SimpleUploadedFile("legacy.pdf", b"x"),
        )
        defaults.update(kw)
        doc = Document.objects.create(**defaults)
        doc.files.all().delete()
        doc.topics.all().delete()
        return doc

    def test_it_creates_one_file_and_one_tag_per_document(self):
        doc = self._legacy(file_hash="hash1")
        self._run()
        self.assertEqual(doc.files.count(), 1)
        self.assertEqual(doc.topics.count(), 1)

    def test_the_file_carries_over_what_the_parent_knew(self):
        doc = self._legacy(file_hash="hash2", filetype="PDF", pages=12)
        self._run()
        f = doc.files.get()
        self.assertEqual(f.filetype, "PDF")
        self.assertEqual(f.pages, 12)
        self.assertEqual(f.file_hash, "hash2")
        self.assertEqual(f.owner_id, self.owner.id)
        self.assertEqual(f.order, 0)

    def test_the_file_gets_a_readable_name(self):
        """`file.name` is a storage path. The redesign's Files list shows a
        filename, and a blank one renders as an unnamed row."""
        doc = self._legacy(file_hash="hash3")
        self._run()
        self.assertTrue(doc.files.get().original_name.endswith(".pdf"))
        self.assertNotIn("/", doc.files.get().original_name)

    def test_the_tag_gets_a_field_derived_from_its_subject(self):
        doc = self._legacy(file_hash="hash4",
                           subject="Civil Engineering", topic="Structural")
        self._run()
        topic = doc.topics.get()
        self.assertEqual(topic.field, "Engineering")
        self.assertEqual(topic.subject, "Civil Engineering")
        self.assertEqual(topic.branch, "Structural")

    def test_a_document_with_no_file_gets_a_tag_but_no_file_row(self):
        """`Document.file` is null=True and the reader falls back to `full`.
        Creating a DocumentFile for those would put a row in the Files list
        pointing at nothing."""
        doc = self._legacy(file="", file_hash="")
        self._run()
        self.assertEqual(doc.files.count(), 0)
        self.assertEqual(doc.topics.count(), 1)

    def test_a_document_with_no_subject_gets_no_tag(self):
        """`subject` is blank on some older rows. Writing a tag with an empty
        subject would add a nameless chip to the browse panel."""
        doc = self._legacy(subject="", topic="", file_hash="hash5")
        self._run()
        self.assertEqual(doc.topics.count(), 0)

    def test_an_invented_subject_lands_in_a_real_field(self):
        """Uploaders invent subjects with no approval, so prod certainly holds
        some that are not in SUBJECT_TOPICS. They must still be browsable."""
        doc = self._legacy(subject="Underwater Basket Weaving",
                           topic="Reeds", file_hash="hash6")
        self._run()
        self.assertEqual(doc.topics.get().field, UNFILED)

    def test_a_removed_document_carries_its_removal_onto_its_file(self):
        """Otherwise the live constraint sees a removed set's file as active
        and blocks its owner from ever re-uploading that file."""
        doc = self._legacy(file_hash="hash7", is_removed=True)
        self._run()
        self.assertTrue(doc.files.get().is_removed)

    def test_reversing_does_not_destroy_a_later_upload(self):
        """⚠ THE POINT. The reverse must undo this migration, not truncate the
        tables. By the time anyone unapplies it, a real multi-file set may
        exist, and its second file was never this migration's to delete."""
        from django.apps import apps

        doc = self._legacy(file_hash="hash8")
        self._run()
        later = DocumentFile.objects.create(
            document=doc, file=SimpleUploadedFile("second.pdf", b"y"),
            original_name="second.pdf", file_hash="hash9", order=1,
        )
        self.mod.backwards(apps, None)
        self.assertTrue(DocumentFile.objects.filter(pk=later.pk).exists())
        self.assertEqual(doc.files.count(), 1)


class SetChildrenSignalTest(TestCase):
    """The other half of the backfill.

    0011 converted the documents that existed when it ran. Everything created
    afterwards — an upload, a Research Hub publication, a row typed into the
    Django admin — has to get the same treatment, or it is born as an empty set
    that renders nothing and appears in no browse facet. Nothing errors; the
    document is simply not there.
    """

    def setUp(self):
        self.owner = _user("signalowner")

    def _doc(self, **kw):
        defaults = dict(
            owner=self.owner, title="Fresh upload",
            subject="Physics", topic="Optics",
            filetype="PDF", pages=3, file_hash="freshhash",
            file=SimpleUploadedFile("fresh.pdf", b"x"),
        )
        defaults.update(kw)
        return Document.objects.create(**defaults)

    def test_a_new_document_gets_its_file_and_tag(self):
        doc = self._doc()
        self.assertEqual(doc.files.count(), 1)
        self.assertEqual(doc.topics.count(), 1)
        self.assertEqual(doc.topics.get().field, "Sciences")

    def test_the_file_row_carries_the_duplicate_hash(self):
        """If this were blank, the constraint that moved onto DocumentFile
        would never fire and the same person could re-upload the same bytes."""
        self.assertEqual(self._doc().files.get().file_hash, "freshhash")

    def test_it_does_not_rewrite_an_existing_set(self):
        """⚠ THE POINT. From Phase 8 a set holds several files and several
        tags, while `Document.file` and `Document.subject` hold only the
        first of each. A signal that kept them "in step" would delete every
        file but the first on the next save of the parent."""
        doc = self._doc()
        second = DocumentFile.objects.create(
            document=doc, file=SimpleUploadedFile("second.pdf", b"y"),
            original_name="second.pdf", file_hash="secondhash", order=1,
        )
        DocumentTopic.objects.create(
            document=doc, field="Social Sciences",
            subject="Sociology", branch="Rural sociology", order=1)

        doc.title = "Renamed"
        doc.save()

        self.assertEqual(doc.files.count(), 2)
        self.assertEqual(doc.topics.count(), 2)
        self.assertTrue(DocumentFile.objects.filter(pk=second.pk).exists())

    def test_a_counter_save_does_nothing(self):
        """Saves that name `update_fields` are moderation flags and counter
        refreshes. They cannot create a set, and this runs on every save."""
        doc = self._doc()
        doc.files.all().delete()
        doc.is_removed = True
        doc.save(update_fields=["is_removed"])
        self.assertEqual(doc.files.count(), 0)

    def test_a_document_with_no_file_still_gets_its_tag(self):
        doc = self._doc(file="", file_hash="")
        self.assertEqual(doc.files.count(), 0)
        self.assertEqual(doc.topics.count(), 1)

    def test_a_document_with_no_subject_gets_no_tag(self):
        doc = self._doc(subject="", topic="")
        self.assertEqual(doc.topics.count(), 0)


class RemovalPropagationTest(TestCase):
    """Deleting a set must free its files for re-upload.

    `DocumentFile.is_removed` is half of the duplicate-upload constraint, and
    it is denormalised from the parent. If it goes stale, "delete it and upload
    the corrected version" stops working permanently — and the refusal names a
    document the owner can no longer see, so it reads as a bug in the uploader
    rather than as leftover state.

    Every removal path is a targeted `save(update_fields=[...])`, which is
    exactly the shape the backfill half of this signal skips. That is how this
    broke the first time.
    """

    def setUp(self):
        self.owner = _user("removalowner")

    def _doc(self, file_hash):
        return Document.objects.create(
            owner=self.owner, title="A set", subject="Physics",
            file=SimpleUploadedFile("x.pdf", b"x"), file_hash=file_hash,
        )

    def test_removing_the_set_marks_its_files_removed(self):
        doc = self._doc("ph1")
        doc.is_removed = True
        doc.save(update_fields=["is_removed"])
        self.assertTrue(doc.files.get().is_removed)

    def test_and_so_the_owner_can_re_upload(self):
        """The behaviour the flag exists for, asserted end to end rather than
        by inspecting the column."""
        doc = self._doc("ph2")
        doc.is_removed = True
        doc.save(update_fields=["is_removed"])
        replacement = Document.objects.create(
            owner=self.owner, title="Corrected",
            file=SimpleUploadedFile("x.pdf", b"x"), file_hash="ph2",
        )
        self.assertEqual(replacement.files.count(), 1)

    def test_restoring_the_set_brings_its_files_back(self):
        """Moderation can restore a removed document. If the files stayed
        marked removed, the restored set would be live with its duplicate slot
        still free — two live copies of the same file under one owner."""
        doc = self._doc("ph3")
        doc.is_removed = True
        doc.save(update_fields=["is_removed"])
        doc.is_removed = False
        doc.save(update_fields=["is_removed"])
        self.assertFalse(doc.files.get().is_removed)

    def test_a_full_save_propagates_too(self):
        """Not every caller passes update_fields."""
        doc = self._doc("ph4")
        doc.is_removed = True
        doc.save()
        self.assertTrue(doc.files.get().is_removed)


class UploadRulesTest(TestCase):
    """Phase 8 — the widened file rules.

    The design accepts "documents, slides, spreadsheets and data, images,
    drawings, code, audio, video and ZIP archives. Up to 200 MB per file."

    What matters here is not that the list got longer. It is that widening an
    allowlist is exactly how a type-validation module stops working: an
    extension added to the list but not to the signature table is allowed
    through on its NAME alone, which is the hole this module was written to
    close.
    """

    def test_every_library_extension_has_a_signature_decision(self):
        """⚠ THE POINT. `validate_upload` only sniffs when a signature
        exists, so a forgotten entry is silently name-only validation. Listing
        a type as None has to be a decision somebody made, not an omission."""
        from config.upload_validation import (
            LIBRARY_EXTS, _OFFSET_SIGNATURES, _SIGNATURES,
        )
        unaccounted = sorted(
            e for e in LIBRARY_EXTS
            if e not in _SIGNATURES and e not in _OFFSET_SIGNATURES)
        self.assertEqual(unaccounted, [])

    def test_markup_is_still_refused(self):
        """/media/ is served back with a Content-Type guessed from the
        extension and auth cookies are scoped to the apex domain, so an
        .html or .svg is stored XSS the moment a moderator opens it. Widening
        for video must not have swept these in."""
        from config.upload_validation import LIBRARY_EXTS
        for ext in (".html", ".htm", ".svg", ".xml", ".xhtml"):
            self.assertNotIn(ext, LIBRARY_EXTS)

    def test_a_real_pdf_passes(self):
        from config.upload_validation import (
            LIBRARY_EXTS, LIBRARY_MAX_MB, validate_upload,
        )
        f = SimpleUploadedFile("a.pdf", b"%PDF-1.4 hello")
        self.assertEqual(
            validate_upload(f, LIBRARY_EXTS, max_mb=LIBRARY_MAX_MB), ".pdf")

    def test_a_disguised_file_is_refused(self):
        from django.core.exceptions import ValidationError
        from config.upload_validation import LIBRARY_EXTS, validate_upload
        f = SimpleUploadedFile("evil.pdf", b"<html><script>alert(1)</script>")
        with self.assertRaises(ValidationError):
            validate_upload(f, LIBRARY_EXTS)

    def test_an_mp4_is_sniffed_at_the_right_offset(self):
        """⚠ ISO base-media files put "ftyp" at byte 4, after a box length.
        A startswith() check is the wrong shape and would reject every real
        mp4 — so the naive fix is to list mp4 as unsignatured, which accepts
        anything named .mp4 instead."""
        from config.upload_validation import LIBRARY_EXTS, validate_upload
        good = SimpleUploadedFile(
            "clip.mp4", b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16)
        self.assertEqual(validate_upload(good, LIBRARY_EXTS), ".mp4")

    def test_and_a_fake_mp4_is_refused(self):
        from django.core.exceptions import ValidationError
        from config.upload_validation import LIBRARY_EXTS, validate_upload
        bad = SimpleUploadedFile("clip.mp4", b"<html>not a video at all</html>")
        with self.assertRaises(ValidationError):
            validate_upload(bad, LIBRARY_EXTS)

    def test_the_other_callers_keep_their_old_limits(self):
        """⚠ DOCUMENT_EXTS is shared with forum attachments and the
        scholarship KYC upload. Neither should start accepting video and
        archives because the Explore library does."""
        from config.upload_validation import DOCUMENT_EXTS
        self.assertEqual(
            DOCUMENT_EXTS,
            {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"})

    def test_the_library_limit_is_50_mb(self):
        """⚠ 50, not the design's 200 — reverted by the owner 2026-11-13.
        Pinned so a later "finish the design" pass cannot quietly put a 200 MB
        ceiling back on a public storage zone that has no backup. The file
        TYPES stay widened; only the size came down."""
        from config.upload_validation import LIBRARY_MAX_MB
        self.assertEqual(LIBRARY_MAX_MB, 50)

    def test_an_oversized_file_is_refused(self):
        from django.core.exceptions import ValidationError
        from config.upload_validation import (
            LIBRARY_EXTS, LIBRARY_MAX_MB, validate_upload,
        )
        big = SimpleUploadedFile("big.pdf", b"%PDF-1.4 " + b"x" * 1024)
        big.size = (LIBRARY_MAX_MB + 1) * 1024 * 1024
        with self.assertRaises(ValidationError) as caught:
            validate_upload(big, LIBRARY_EXTS, max_mb=LIBRARY_MAX_MB)
        self.assertIn("50 MB", str(caught.exception))
