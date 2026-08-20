"""Tests for the Explore document library + its moderation panel.

Run with: DJANGO_SETTINGS_MODULE=config.settings_test ... manage.py test documents
"""
import json

from django.test import TestCase, Client
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User, Role, UserRole, Permission
from documents.models import (
    Document, DocumentCategory, Report, DocumentProfile, ModerationAction,
    DuplicateFlag, SavedDocument,
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
