"""Tests for the Explore document library + its moderation panel.

Run with: DJANGO_SETTINGS_MODULE=config.settings_test ... manage.py test documents
"""
import json
import os

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User, Role, UserRole, Permission
from documents.models import (
    Document, DocumentCategory, Report, DocumentProfile, ModerationAction,
    DuplicateFlag, SavedDocument, Collection,
)


def auth_client(user):
    c = Client()
    c.cookies["access"] = str(RefreshToken.for_user(user).access_token)
    return c


class DocumentsRBACTests(TestCase):
    def test_seed_created_documents_permissions(self):
        # 0028 adds 10 documents.* perms on top of 0027's 13.
        self.assertEqual(Permission.objects.filter(codename__startswith="documents.").count(), 10)

    def test_moderator_role_holds_documents_moderate(self):
        u = User.objects.create(email="m@t.com", username="m")
        UserRole.objects.create(user=u, role=Role.objects.get(name="MODERATOR"))
        u = User.objects.get(pk=u.pk)
        self.assertTrue(u.has_permission("documents.moderate"))
        self.assertTrue(u.has_permission("documents.uploaders.ban"))


class ExplorePublicTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="o@t.com", username="owner", is_verified=True)
        self.reader = User.objects.create(email="r@t.com", username="reader", is_verified=True)
        # "notes" is seeded by migration 0002 — reuse it rather than colliding.
        self.cat, _ = DocumentCategory.objects.get_or_create(slug="notes", defaults={"name": "Notes"})
        self.doc = Document.objects.create(
            owner=self.owner, title="Linear Algebra Notes", category=self.cat,
            subject="Mathematics", filetype="PDF")

    def test_landing_and_facets_public(self):
        c = Client()
        self.assertEqual(c.get("/api/explore/facets/").status_code, 200)
        landing = c.get("/api/explore/landing/")
        self.assertEqual(landing.status_code, 200)
        self.assertIn("categories", landing.json())
        self.assertIn("trendChips", landing.json())

    def test_search_lists_document(self):
        c = Client()
        r = c.get("/api/explore/documents/?q=algebra")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 1)

    def test_search_by_ids(self):
        d2 = Document.objects.create(owner=self.owner, title="Second doc")
        r = Client().get(f"/api/explore/documents/?ids={self.doc.id},{d2.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 2)

    def test_search_by_empty_ids_returns_nothing(self):
        # A user with nothing saved hits this with `ids=` (empty) — must
        # NOT fall through to the general listing (which would show every
        # published document instead of none).
        r = Client().get("/api/explore/documents/?ids=")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)
        self.assertEqual(r.json()["results"], [])

    def test_upload_creates_document(self):
        c = auth_client(self.reader)
        r = c.post("/api/explore/documents/",
                   data={"title": "My paper", "category": "notes", "tags": "ai,ml"})
        self.assertEqual(r.status_code, 201)
        self.assertTrue(Document.objects.filter(title="My paper", owner=self.reader).exists())

    def test_upload_rejects_html_disguised_as_pdf(self):
        # Previously accepted ANY file with no server-side type check at all
        # — a stored-XSS risk once served back with a guessed Content-Type
        # from the /media/ origin that shares the auth cookie's domain.
        from django.core.files.uploadedfile import SimpleUploadedFile
        c = auth_client(self.reader)
        payload = SimpleUploadedFile(
            "notes.pdf", b"<html><script>alert(1)</script></html>",
            content_type="application/pdf",
        )
        r = c.post("/api/explore/documents/",
                   data={"title": "Fake PDF", "file": payload})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Document.objects.filter(title="Fake PDF").exists())

    def test_upload_rejects_disallowed_extension(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        c = auth_client(self.reader)
        payload = SimpleUploadedFile("evil.svg", b"<svg onload=alert(1)></svg>", content_type="image/svg+xml")
        r = c.post("/api/explore/documents/",
                   data={"title": "SVG upload", "file": payload})
        self.assertEqual(r.status_code, 400)

    def test_upload_accepts_real_pdf(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        c = auth_client(self.reader)
        payload = SimpleUploadedFile("real.pdf", b"%PDF-1.4\n...", content_type="application/pdf")
        r = c.post("/api/explore/documents/",
                   data={"title": "Real PDF", "file": payload})
        self.assertEqual(r.status_code, 201)
        self.assertTrue(Document.objects.filter(title="Real PDF").exists())

    def test_toggle_save(self):
        c = auth_client(self.reader)
        r = c.post(f"/api/explore/documents/{self.doc.id}/save/")
        self.assertTrue(r.json()["saved"])
        self.assertEqual(SavedDocument.objects.filter(user=self.reader).count(), 1)
        r = c.post(f"/api/explore/documents/{self.doc.id}/save/")
        self.assertFalse(r.json()["saved"])

    def test_self_report_blocked(self):
        c = auth_client(self.owner)
        r = c.post(f"/api/explore/documents/{self.doc.id}/report/",
                   data=json.dumps({"reason": "copyright"}),
                   content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_duplicate_report_deduped(self):
        c = auth_client(self.reader)
        payload = json.dumps({"reason": "plagiarism"})
        self.assertEqual(c.post(f"/api/explore/documents/{self.doc.id}/report/",
                                data=payload, content_type="application/json").status_code, 201)
        self.assertEqual(c.post(f"/api/explore/documents/{self.doc.id}/report/",
                                data=payload, content_type="application/json").status_code, 200)
        self.assertEqual(Report.objects.filter(resolved=False).count(), 1)

    def test_removed_document_hidden_from_search(self):
        self.doc.is_removed = True
        self.doc.save(update_fields=["is_removed"])
        r = Client().get("/api/explore/documents/?q=algebra")
        self.assertEqual(r.json()["count"], 0)

    def test_me_exposes_permissions(self):
        r = auth_client(self.reader).get("/api/explore/me/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("is_moderator", r.json())
        self.assertIn("permissions", r.json())
        self.assertFalse(r.json()["is_moderator"])


class ExploreModerationTests(TestCase):
    def setUp(self):
        self.mod = User.objects.create(email="mod@t.com", username="mod", is_verified=True)
        UserRole.objects.create(user=self.mod, role=Role.objects.get(name="MODERATOR"))
        self.owner = User.objects.create(email="o@t.com", username="owner", is_verified=True)
        self.reporter = User.objects.create(email="rp@t.com", username="rp", is_verified=True)
        self.doc = Document.objects.create(owner=self.owner, title="Scanned textbook")
        self.report = Report.objects.create(
            reporter=self.reporter, target=self.doc, reason="copyright")

    def test_non_moderator_blocked(self):
        r = auth_client(self.owner).get("/api/explore/mod/reports/")
        self.assertEqual(r.status_code, 403)

    def test_moderator_sees_reports(self):
        r = auth_client(self.mod).get("/api/explore/mod/reports/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 1)
        self.assertEqual(r.json()["results"][0]["content_title"], "Scanned textbook")

    def test_remove_document_soft_deletes_and_logs(self):
        c = auth_client(self.mod)
        r = c.post(f"/api/explore/mod/reports/{self.report.id}/remove/",
                   data=json.dumps({"note": "verbatim copy"}),
                   content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.doc.refresh_from_db()
        self.assertTrue(self.doc.is_removed)
        self.report.refresh_from_db()
        self.assertTrue(self.report.resolved)
        self.assertTrue(ModerationAction.objects.filter(
            action=ModerationAction.ACTION_REMOVE, target_user=self.owner).exists())

    def test_ban_uploader(self):
        c = auth_client(self.mod)
        r = c.post(f"/api/explore/mod/uploaders/{self.owner.id}/ban/",
                   data=json.dumps({"note": "repeat offender"}),
                   content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(DocumentProfile.objects.get(user=self.owner).is_banned)

    def test_banned_uploader_cannot_upload(self):
        DocumentProfile.objects.create(user=self.owner, is_banned=True)
        r = auth_client(self.owner).post("/api/explore/documents/", data={"title": "x"})
        self.assertEqual(r.status_code, 403)

    def test_duplicate_review_flow(self):
        dup = Document.objects.create(owner=self.owner, title="Scanned textbook copy")
        flag = DuplicateFlag.objects.create(document=dup, original=self.doc, similarity=95)
        c = auth_client(self.mod)
        self.assertEqual(c.get("/api/explore/mod/duplicates/").json()["count"], 1)
        r = c.post(f"/api/explore/mod/duplicates/{flag.id}/confirm/")
        self.assertEqual(r.status_code, 200)
        dup.refresh_from_db()
        self.assertTrue(dup.is_removed)

    def test_analytics_header_stats_shape(self):
        r = auth_client(self.mod).get("/api/explore/mod/analytics/")
        self.assertEqual(r.status_code, 200)
        hs = r.json()["header_stats"]
        self.assertIn("reported_docs", hs)
        self.assertIn("duplicate_uploads", hs)
        self.assertIn("uploads_published", r.json()["this_month"])


# =====================================================
# Phase 1 — duplicate guard, owner delete, ?mine=1,
# private-collection privacy on the author page.
# =====================================================
class DuplicateUploadTests(TestCase):
    """The same person may not upload the same bytes twice. Two different
    people may — that's what the moderators' Duplicate Review queue is for."""

    def setUp(self):
        self.a = User.objects.create(email="dupa@t.com", username="dupa", is_verified=True)
        self.b = User.objects.create(email="dupb@t.com", username="dupb", is_verified=True)

    def _pdf(self, body=b"%PDF-1.4 identical bytes", name="paper.pdf"):
        return SimpleUploadedFile(name, body, content_type="application/pdf")

    def test_same_file_twice_by_same_user_is_refused(self):
        c = auth_client(self.a)
        first = c.post("/api/explore/documents/", data={"title": "Thermo notes", "file": self._pdf()})
        self.assertEqual(first.status_code, 201)

        second = c.post("/api/explore/documents/", data={"title": "Thermo notes again", "file": self._pdf()})
        self.assertEqual(second.status_code, 409)
        # The message has to name the document they already have, or "duplicate"
        # is a dead end for the person trying to publish.
        self.assertIn("Thermo notes", second.json()["detail"])
        self.assertEqual(second.json()["duplicate_of"], first.json()["id"])
        self.assertEqual(Document.objects.filter(owner=self.a, is_removed=False).count(), 1)

    def test_renaming_the_file_does_not_defeat_the_check(self):
        c = auth_client(self.a)
        c.post("/api/explore/documents/", data={"title": "One", "file": self._pdf(name="a.pdf")})
        again = c.post("/api/explore/documents/", data={"title": "Two", "file": self._pdf(name="b.pdf")})
        self.assertEqual(again.status_code, 409)

    def test_different_users_may_upload_the_same_file(self):
        self.assertEqual(
            auth_client(self.a).post(
                "/api/explore/documents/", data={"title": "Shared paper", "file": self._pdf()}
            ).status_code, 201)
        self.assertEqual(
            auth_client(self.b).post(
                "/api/explore/documents/", data={"title": "Shared paper", "file": self._pdf()}
            ).status_code, 201)

    def test_different_bytes_are_not_duplicates(self):
        c = auth_client(self.a)
        c.post("/api/explore/documents/", data={"title": "V1", "file": self._pdf(b"%PDF-1.4 one")})
        r = c.post("/api/explore/documents/", data={"title": "V2", "file": self._pdf(b"%PDF-1.4 two")})
        self.assertEqual(r.status_code, 201)

    def test_documents_without_a_file_never_collide(self):
        """Blank hashes must not match each other, or the second fileless
        upload would be refused as a duplicate of the first."""
        c = auth_client(self.a)
        self.assertEqual(c.post("/api/explore/documents/", data={"title": "No file 1"}).status_code, 201)
        self.assertEqual(c.post("/api/explore/documents/", data={"title": "No file 2"}).status_code, 201)

    def test_the_stored_file_is_not_truncated_by_hashing(self):
        """Hashing reads the handle to EOF; if it isn't rewound Django stores
        zero bytes and the upload silently succeeds as an empty file."""
        body = b"%PDF-1.4 " + b"x" * 5000
        r = auth_client(self.a).post(
            "/api/explore/documents/", data={"title": "Full", "file": self._pdf(body)})
        self.assertEqual(r.status_code, 201)
        doc = Document.objects.get(pk=r.json()["id"])
        self.assertEqual(doc.file.size, len(body))


class OwnerDeleteTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="od@t.com", username="odowner", is_verified=True)
        self.other = User.objects.create(email="oo@t.com", username="odother", is_verified=True)
        self.staff = User.objects.create(email="os@t.com", username="odstaff", is_verified=True, is_staff=True)

    def _upload(self, user, title="Mine", body=b"%PDF-1.4 delete me"):
        r = auth_client(user).post("/api/explore/documents/", data={
            "title": title, "file": SimpleUploadedFile("d.pdf", body, content_type="application/pdf")})
        self.assertEqual(r.status_code, 201)
        return Document.objects.get(pk=r.json()["id"])

    def test_owner_can_delete_and_it_leaves_the_library(self):
        doc = self._upload(self.owner)
        r = auth_client(self.owner).delete(f"/api/explore/documents/{doc.id}/")
        self.assertEqual(r.status_code, 204)
        doc.refresh_from_db()
        self.assertTrue(doc.is_removed)
        self.assertIsNotNone(doc.removed_at)
        self.assertEqual(auth_client(self.owner).get(f"/api/explore/documents/{doc.id}/").status_code, 404)

    def test_delete_removes_the_stored_bytes(self):
        """Explore media sits in a public bucket, so "deleted" has to mean the
        file stops being retrievable — not just unlisted."""
        doc = self._upload(self.owner)
        path = doc.file.path
        self.assertTrue(os.path.exists(path))
        auth_client(self.owner).delete(f"/api/explore/documents/{doc.id}/")
        self.assertFalse(os.path.exists(path))

    def test_a_stranger_cannot_delete_someone_elses_upload(self):
        doc = self._upload(self.owner)
        self.assertEqual(
            auth_client(self.other).delete(f"/api/explore/documents/{doc.id}/").status_code, 403)
        doc.refresh_from_db()
        self.assertFalse(doc.is_removed)

    def test_anonymous_cannot_delete(self):
        doc = self._upload(self.owner)
        self.assertEqual(Client().delete(f"/api/explore/documents/{doc.id}/").status_code, 401)

    def test_staff_can_delete(self):
        doc = self._upload(self.owner)
        self.assertEqual(
            auth_client(self.staff).delete(f"/api/explore/documents/{doc.id}/").status_code, 204)

    def test_deleting_frees_the_file_for_re_upload(self):
        """Otherwise "delete and re-upload the corrected version" is impossible
        — the duplicate guard would refuse the replacement forever."""
        body = b"%PDF-1.4 re-uploadable"
        doc = self._upload(self.owner, body=body)
        auth_client(self.owner).delete(f"/api/explore/documents/{doc.id}/")
        again = auth_client(self.owner).post("/api/explore/documents/", data={
            "title": "Corrected", "file": SimpleUploadedFile("d.pdf", body, content_type="application/pdf")})
        self.assertEqual(again.status_code, 201)


class MyUploadsTests(TestCase):
    def setUp(self):
        self.me = User.objects.create(email="mu@t.com", username="mume", is_verified=True)
        self.them = User.objects.create(email="mt@t.com", username="muthem", is_verified=True)
        self.mine = Document.objects.create(owner=self.me, title="Mine A")
        Document.objects.create(owner=self.me, title="Mine removed", is_removed=True)
        Document.objects.create(owner=self.them, title="Theirs")

    def test_mine_returns_only_my_live_uploads(self):
        r = auth_client(self.me).get("/api/explore/documents/?mine=1")
        self.assertEqual(r.status_code, 200)
        titles = [d["title"] for d in r.json()["results"]]
        self.assertEqual(titles, ["Mine A"])
        self.assertEqual(r.json()["count"], 1)

    def test_mine_requires_authentication(self):
        self.assertEqual(Client().get("/api/explore/documents/?mine=1").status_code, 401)

    def test_mine_is_not_confused_with_a_normal_listing(self):
        """Without the param the listing must still show everyone's documents —
        a truthy-check bug here would silently scope the whole library."""
        r = auth_client(self.me).get("/api/explore/documents/")
        self.assertEqual(r.json()["count"], 2)


class AuthorCollectionPrivacyTests(TestCase):
    def setUp(self):
        self.author = User.objects.create(email="ac@t.com", username="acauthor", is_verified=True)
        self.visitor = User.objects.create(email="av@t.com", username="acvisitor", is_verified=True)
        Collection.objects.create(curator=self.author, title="Public shelf",
                                  visibility=Collection.VIS_PUBLIC)
        Collection.objects.create(curator=self.author, title="Private shelf",
                                  visibility=Collection.VIS_PRIVATE)

    def _titles(self, response):
        return sorted(c["title"] for c in response.json()["collections"])

    def test_a_visitor_sees_only_public_collections(self):
        r = Client().get("/api/explore/authors/acauthor/")
        self.assertEqual(self._titles(r), ["Public shelf"])

    def test_another_signed_in_user_sees_only_public_collections(self):
        r = auth_client(self.visitor).get("/api/explore/authors/acauthor/")
        self.assertEqual(self._titles(r), ["Public shelf"])

    def test_the_author_sees_their_own_private_collections(self):
        """The count on your own profile must match your own My Library, which
        is the mismatch that surfaced this."""
        r = auth_client(self.author).get("/api/explore/authors/acauthor/")
        self.assertEqual(self._titles(r), ["Private shelf", "Public shelf"])


class ViewCountTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(email="vc@t.com", username="vcowner", is_verified=True)
        self.doc = Document.objects.create(owner=self.owner, title="Counted")

    def test_view_endpoint_increments_and_reports_the_new_total(self):
        r = Client().post(f"/api/explore/documents/{self.doc.id}/view/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["views"], 1)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.view_count, 1)

    def test_increments_do_not_overwrite_each_other(self):
        """Read-then-write lost concurrent increments; F() must not."""
        Document.objects.filter(pk=self.doc.pk).update(view_count=41)
        r = Client().post(f"/api/explore/documents/{self.doc.id}/view/")
        self.assertEqual(r.json()["views"], 42)

    def test_download_endpoint_increments(self):
        r = Client().post(f"/api/explore/documents/{self.doc.id}/download/")
        self.assertEqual(r.json()["downloads"], 1)
