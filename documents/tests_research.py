"""Research workflow permissions, revision binding, and publication regression tests."""
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, override_settings, Client
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from accounts.models import User, UserRole, Role
from .models import ResearchSubmission as Paper, ResearchReviewer as Reviewer, ResearchAssignment as Assignment, ResearchRevision as Revision, Document
from .tests import auth_client


class ResearchWorkflowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tempfile.mkdtemp(prefix="shiksha-research-tests-")
        cls.settings_override = override_settings(RESEARCH_PRIVATE_ROOT=cls.files+"/private", MEDIA_ROOT=cls.files+"/public")
        cls.settings_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.settings_override.disable()
        shutil.rmtree(cls.files, ignore_errors=True)

    def setUp(self):
        self.author = User.objects.create_user(username="author-r", email="author-r@test.example", password="example")
        self.teacher = User.objects.create_user(username="reviewer-r", email="reviewer-r@test.example", password="example")
        self.other = User.objects.create_user(username="other-r", email="other-r@test.example", password="example")
        self.admin = User.objects.create_user(username="admin-r", email="admin-r@test.example", password="example", is_staff=True)
        UserRole.objects.create(user=self.admin, role=Role.objects.get(name="ADMIN"))
        self.a, self.t, self.o, self.m = [auth_client(u) for u in (self.author, self.teacher, self.other, self.admin)]
        self.profile = Reviewer.objects.create(user=self.teacher, subjects=["Mathematics"], expertise="Research methods", approved=True, approved_by=self.admin)

    def pdf(self, body=b"research version one"):
        return SimpleUploadedFile("paper.pdf", b"%PDF-1.4\n"+body, content_type="application/pdf")

    def submit(self):
        result = self.a.post("/api/explore/research/", {"title": "Algebra research", "abstract": "Methods and results", "authors": "Author R",
            "subject": "Mathematics", "topic": "Algebra", "file": self.pdf(), "permission_confirmed": "true"})
        self.assertEqual(result.status_code, 201, result.content)
        self.paper_id = result.json()["id"]
        return result.json()

    def action(self, client, action, **kwargs):
        if action == "review":
            kwargs.setdefault("checks", {k: "checked" for k in ("relevance", "clarity", "methods", "evidence", "references")})
        return client.post(f"/api/explore/research/{self.paper_id}/", {"action": action, "version": kwargs.pop("version", 1), **kwargs}, content_type="application/json")

    def assign(self):
        result = self.action(self.m, "assign", reviewer_id=self.teacher.pk)
        self.assertEqual(result.status_code, 200, result.content)

    def accept(self):
        result = self.action(self.t, "accept", expertise_confirmed=True, no_conflict=True, confidential=True)
        self.assertEqual(result.status_code, 200, result.content)

    def approve(self):
        self.assign(); self.accept()
        result = self.action(self.t, "review", decision="approve", note="Methods and references checked; recommend publication.")
        self.assertEqual(result.status_code, 200, result.content)

    def test_end_to_end_private_to_reviewed_publication(self):
        data = self.submit()
        self.assertFalse(Document.objects.filter(title=data["title"]).exists())
        self.approve()
        result = self.action(self.m, "publish")
        self.assertEqual(result.status_code, 200, result.content)
        doc = Document.objects.get(pk=result.json()["document_id"])
        self.assertEqual(doc.subject, "Mathematics")
        public = Client().get(f"/api/explore/documents/{doc.pk}/").json()
        self.assertEqual(public["doc"]["research"]["version"], 1)
        self.assertEqual(self.action(self.m, "publish").status_code, 400)
        self.assertEqual(Document.objects.filter(title=data["title"]).count(), 1)

    def test_private_manuscript_is_not_public_or_accessible_to_stranger(self):
        data = self.submit()
        url = "/api"+data["revisions"][0]["file_path"]
        self.assertEqual(self.a.get(url).status_code, 200)
        self.assertEqual(self.o.get(url).status_code, 404)
        self.assertIn(Client().get(url).status_code, (401, 403))
        self.assertEqual(self.o.get(f"/api/explore/research/{self.paper_id}/").status_code, 404)
        revision = Revision.objects.get(submission_id=self.paper_id)
        with self.assertRaises(ValueError):
            _ = revision.file.url
        public_list = Client().get("/api/explore/documents/?q=Algebra%20research").json()
        self.assertEqual(public_list["count"], 0)
        self.assertEqual(self.o.get("/api/explore/research/?scope=admin").status_code, 403)

    def test_invited_reviewer_sees_abstract_but_not_manuscript_until_accept(self):
        data = self.submit(); self.assign()
        url = "/api"+data["revisions"][0]["file_path"]
        before = self.t.get(f"/api/explore/research/{self.paper_id}/").json()
        self.assertNotIn("revisions", before)
        self.assertEqual(self.t.get(url).status_code, 403)
        self.assertEqual(self.action(self.t, "accept").status_code, 400)
        self.accept()
        response = self.t.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_only_assigned_approved_reviewer_can_review(self):
        self.submit(); self.assign()
        self.assertEqual(self.action(self.o, "review", decision="approve", note="ok").status_code, 403)
        self.assertEqual(self.action(self.t, "review", decision="approve", note="ok").status_code, 400)
        self.profile.approved = False; self.profile.save()
        self.assertEqual(self.action(self.t, "accept", expertise_confirmed=True, no_conflict=True, confidential=True).status_code, 403)

    def test_revisions_invalidate_old_approval_and_keep_files(self):
        self.submit(); self.approve()
        result = self.action(self.m, "changes", note="Clarify the statistical method.")
        self.assertEqual(result.status_code, 200)
        revised = self.a.post(f"/api/explore/research/{self.paper_id}/revisions/", {"file": self.pdf(b"new methods"), "response": "Added method details", "permission_confirmed": "true"})
        self.assertEqual(revised.status_code, 201, revised.content)
        self.assertEqual(len(revised.json()["revisions"]), 2)
        self.assertEqual(self.action(self.m, "publish", version=2).status_code, 400)
        self.assertEqual(self.action(self.m, "assign", version=1, reviewer_id=self.teacher.pk).status_code, 400)
        self.assertEqual(self.action(self.m, "assign", version=2, reviewer_id=self.teacher.pk).status_code, 200)
        self.assertEqual(self.action(self.t, "accept", version=2, expertise_confirmed=True, no_conflict=True, confidential=True).status_code, 200)
        self.assertEqual(self.action(self.t, "review", version=2, decision="approve", note="Revision addresses concerns").status_code, 200)
        self.assertEqual(self.action(self.m, "publish", version=2).status_code, 200)

    def test_author_cannot_assign_or_publish(self):
        self.submit()
        self.assertEqual(self.action(self.a, "assign", reviewer_id=self.teacher.pk).status_code, 403)
        self.approve()
        self.assertEqual(self.action(self.a, "publish").status_code, 403)

    def test_wrong_subject_and_self_review_refused(self):
        self.submit()
        self.profile.subjects = ["Physics"]; self.profile.save()
        self.assertEqual(self.action(self.m, "assign", reviewer_id=self.teacher.pk).status_code, 400)
        Reviewer.objects.create(user=self.author, subjects=["Mathematics"], expertise="Author", approved=True)
        self.assertEqual(self.action(self.m, "assign", reviewer_id=self.author.pk).status_code, 400)

    def test_reassignment_revokes_old_reviewer_access(self):
        data = self.submit(); self.assign(); self.accept()
        Reviewer.objects.create(user=self.other, subjects=["Mathematics"], expertise="Another specialist", approved=True)
        self.assertEqual(self.action(self.m, "assign", reviewer_id=self.other.pk).status_code, 200)
        self.assertEqual(self.t.get("/api"+data["revisions"][0]["file_path"]).status_code, 404)
        self.assertEqual(self.action(self.t, "review", decision="approve", note="old").status_code, 403)

    def test_decline_returns_to_assignment_queue(self):
        self.submit(); self.assign()
        self.assertEqual(self.action(self.t, "decline", note="Unavailable").json()["status"], "submitted")
        self.assertEqual(self.action(self.t, "accept").status_code, 403)

    def test_review_requires_reason_and_publication_requires_approval(self):
        self.submit(); self.assign(); self.accept()
        self.assertEqual(self.action(self.t, "review", decision="approve").status_code, 400)
        self.assertEqual(self.action(self.m, "publish").status_code, 400)
        self.assertEqual(self.action(self.t, "review", decision="reject", note="Methods unsupported").status_code, 200)
        self.assertEqual(self.action(self.m, "publish").status_code, 400)
        self.assertEqual(self.action(self.m, "reject", note="Review confirms unsuitable methods").status_code, 200)

    def test_author_can_withdraw_unpublished_and_cannot_revise_without_request(self):
        self.submit()
        self.assertEqual(self.a.post(f"/api/explore/research/{self.paper_id}/revisions/", {"file": self.pdf(), "permission_confirmed": "true"}).status_code, 400)
        self.assertEqual(self.action(self.a, "withdraw", note="Need further work").status_code, 200)
        self.assertEqual(self.action(self.m, "assign", reviewer_id=self.teacher.pk).status_code, 400)

    def test_reviewer_application_resets_approval_on_expertise_change(self):
        result = self.t.post("/api/explore/research/reviewers/", {"expertise": "New field", "subjects": ["Physics"]}, content_type="application/json")
        self.assertEqual(result.status_code, 201)
        self.assertFalse(result.json()["approved"])
        self.assertEqual(self.o.patch(f"/api/explore/research/reviewers/{self.profile.pk}/", {"approved": True}, content_type="application/json").status_code, 403)

    def test_admin_reviewer_revocation_reopens_queue_and_blocks_file(self):
        data = self.submit(); self.approve()
        result = self.m.patch(f"/api/explore/research/reviewers/{self.profile.pk}/", {"approved": False}, content_type="application/json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Paper.objects.get(pk=self.paper_id).status, "submitted")
        self.assertEqual(self.t.get("/api"+data["revisions"][0]["file_path"]).status_code, 404)
        self.assertEqual(self.action(self.m, "publish").status_code, 400)

    def test_general_upload_cannot_bypass_research_review(self):
        response = self.a.post("/api/explore/documents/", {"title": "Bypass", "category": "research-papers", "file": self.pdf()})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Document.objects.filter(title="Bypass").exists())

    def test_files_and_permission_validation(self):
        for file in [SimpleUploadedFile("paper.pdf", b"<html>bad</html>"), SimpleUploadedFile("paper.html", b"%PDF-1.4")]:
            response = self.a.post("/api/explore/research/", {"title": "Bad", "abstract": "x", "authors": "x", "subject": "Math", "file": file, "permission_confirmed": "true"})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.a.post("/api/explore/research/", {"title": "Bad", "abstract": "x", "authors": "x", "subject": "Math", "file": self.pdf(), "permission_confirmed": "false"}).status_code, 400)

    def test_duplicate_submission_and_identical_revision_refused(self):
        self.submit()
        again = self.a.post("/api/explore/research/", {"title": "Again", "abstract": "x", "authors": "x", "subject": "Math", "file": self.pdf(), "permission_confirmed": "true"})
        self.assertEqual(again.status_code, 400)
        self.action(self.m, "changes", note="Revise methods")
        self.assertEqual(self.a.post(f"/api/explore/research/{self.paper_id}/revisions/", {"file": self.pdf(), "response": "done", "permission_confirmed": "true"}).status_code, 400)

    def test_notification_dispatch_waits_for_commit(self):
        with patch("notifications.services.notify") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self.submit()
                self.assertFalse(notify.called)
            self.assertEqual(notify.call_count, 2)
            self.assertTrue(all(c.kwargs["verb"] == "research.updated" for c in notify.call_args_list))

    def test_database_prevents_two_active_review_assignments(self):
        self.submit(); self.assign()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Assignment.objects.create(submission_id=self.paper_id, reviewer=self.other,
                revision=Revision.objects.get(submission_id=self.paper_id), due_at=timezone.now()+timedelta(days=7))

    def test_review_checklist_required_and_unresolved_issues_prevent_approval(self):
        self.submit(); self.assign(); self.accept()
        self.assertEqual(self.action(self.t, "review", decision="approve", note="Check", checks={}).status_code, 400)
        checks = {k: "checked" for k in ("relevance", "clarity", "methods", "evidence", "references")}
        checks["methods"] = "issues"
        self.assertEqual(self.action(self.t, "review", decision="approve", note="Check", checks=checks).status_code, 400)

    def test_withdrawal_removes_public_copy_but_keeps_private_revision(self):
        data = self.submit(); self.approve()
        result = self.action(self.m, "publish")
        doc = Document.objects.get(pk=result.json()["document_id"])
        self.assertTrue(doc.file.storage.exists(doc.file.name))
        self.assertEqual(self.action(self.a, "withdraw", note="Author request").status_code, 403)
        with self.captureOnCommitCallbacks(execute=True), patch("notifications.services.notify"):
            self.assertEqual(self.action(self.m, "withdraw", note="Correction needed").status_code, 200)
        self.assertFalse(doc.file.storage.exists(doc.file.name))
        self.assertTrue(Revision.objects.get(submission_id=self.paper_id).file.storage.exists(Revision.objects.get(submission_id=self.paper_id).file.name))
        self.assertEqual(Client().get(f"/api/explore/documents/{doc.pk}/").status_code, 404)

    def test_private_storage_cannot_be_configured_under_public_media(self):
        from .checks import research_storage_check
        with override_settings(RESEARCH_PRIVATE_ROOT=self.files+"/public/manuscripts"):
            self.assertEqual([e.id for e in research_storage_check(None)], ["documents.E001"])

    def test_generic_document_delete_cannot_bypass_research_withdrawal(self):
        self.submit(); self.approve()
        result = self.action(self.m, "publish")
        doc_id = result.json()["document_id"]
        self.assertEqual(self.a.delete(f"/api/explore/documents/{doc_id}/").status_code, 403)
        self.assertFalse(Document.objects.get(pk=doc_id).is_removed)

    def test_content_staff_without_admin_role_cannot_access_research_admin(self):
        self.submit()
        self.other.is_staff = True
        self.other.save()
        staff = auth_client(self.other)
        self.assertEqual(staff.get("/api/explore/research/?scope=admin").status_code, 403)
        self.assertEqual(staff.get(f"/api/explore/research/{self.paper_id}/").status_code, 404)
