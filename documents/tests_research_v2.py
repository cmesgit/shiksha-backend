import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, override_settings, Client
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from accounts.models import User, Role, UserRole
from .models import (ResearchSubmission as Paper, ResearchReviewer as Reviewer,
    ResearchAssignment as Assignment, ResearchRevision as Revision, ResearchCase as Case, Document)
from .tests import auth_client
from .tasks import sweep_research_reviews


@override_settings(RESEARCH_DEFAULT_REVIEWS=2, RESEARCH_AUTO_MATCHING=True,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ResearchV2Tests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tempfile.mkdtemp(prefix="research-v2-tests-")
        cls.file_settings = override_settings(RESEARCH_PRIVATE_ROOT=cls.files+"/private", MEDIA_ROOT=cls.files+"/public")
        cls.file_settings.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass(); cls.file_settings.disable(); shutil.rmtree(cls.files, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user(username="v2-author", email="v2-author@test.example")
        self.admin = User.objects.create_user(username="v2-admin", email="v2-admin@test.example", is_staff=True)
        UserRole.objects.create(user=self.admin, role=Role.objects.get(name="ADMIN"))
        self.a, self.m = auth_client(self.owner), auth_client(self.admin)
        self.reviewers = []
        for n in range(5):
            user = User.objects.create_user(username=f"v2-reviewer-{n}", email=f"v2-reviewer-{n}@test.example")
            Reviewer.objects.create(user=user, expertise="Independent subject specialist", subjects=["Mathematics"], topics=["Algebra"] if n == 0 else [], approved=True)
            self.reviewers.append(user)
        self.checks = {k: "checked" for k in ("relevance", "clarity", "methods", "evidence", "references")}

    def pdf(self, text=b"original"):
        return SimpleUploadedFile("paper.pdf", b"%PDF-1.4\n"+text, content_type="application/pdf")

    def submit(self):
        result = self.a.post("/api/explore/research/", {"title": "Algebra v2", "abstract": "Research abstract", "authors": "Test Author", "subject": "Mathematics", "topic": "Algebra", "permission_confirmed": "true", "file": self.pdf()})
        self.assertEqual(result.status_code, 201, result.content)
        self.pk = result.json()["id"]
        return result.json()

    def action(self, client, action, **kwargs):
        version = Paper.objects.get(pk=self.pk).current_version
        if action == "review":
            kwargs.setdefault("checks", self.checks); kwargs.setdefault("note", "Methods, evidence and references checked.")
        return client.post(f"/api/explore/research/{self.pk}/", {"action": action, "version": version, **kwargs}, content_type="application/json")

    def accepts(self, user):
        result = self.action(auth_client(user), "accept", expertise_confirmed=True, no_conflict=True, confidential=True)
        self.assertEqual(result.status_code, 200, result.content)
        return result

    def accept_two(self):
        users = [a.reviewer for a in Assignment.objects.filter(submission_id=self.pk, active=True, status="invited").select_related("reviewer").order_by("pk")[:2]]
        for user in users: self.accepts(user)
        return users

    def approve_two(self):
        users = self.accept_two()
        for user in users:
            result = self.action(auth_client(user), "review", decision="approve")
            self.assertEqual(result.status_code, 200, result.content)
        return users

    def publish(self):
        self.approve_two()
        result = self.action(self.m, "publish")
        self.assertEqual(result.status_code, 200, result.content)
        return result.json()

    def correction(self):
        result = self.a.post(f"/api/explore/research/{self.pk}/corrections/", {"file": self.pdf(b"corrected"), "permission_confirmed": "true", "response": "Correct the numerical result in section two."})
        self.assertEqual(result.status_code, 201, result.content)
        return result.json()

    def test_new_submission_matches_subject_and_prefers_topic(self):
        self.submit()
        rows = list(Assignment.objects.filter(submission_id=self.pk).order_by("pk"))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].reviewer, self.reviewers[0])
        paper = Paper.objects.get(pk=self.pk)
        self.assertEqual(paper.required_reviews, 2); self.assertTrue(paper.automatic_matching)
        self.assertEqual(paper.status, "invited")
        self.assertTrue(all(abs((row.due_at-timezone.now()).total_seconds()-48*3600)<10 for row in rows))

    def test_matching_excludes_owner_unapproved_unavailable_and_wrong_subject(self):
        Reviewer.objects.create(user=self.owner, expertise="Author", subjects=["Mathematics"], approved=True)
        Reviewer.objects.filter(user=self.reviewers[0]).update(approved=False)
        Reviewer.objects.filter(user=self.reviewers[1]).update(available=False)
        Reviewer.objects.filter(user=self.reviewers[2]).update(subjects=["Physics"])
        self.submit()
        self.assertEqual(set(Assignment.objects.filter(submission_id=self.pk).values_list("reviewer_id", flat=True)), {self.reviewers[3].pk, self.reviewers[4].pk})

    def test_matching_respects_workload_limit(self):
        other = Paper.objects.create(owner=self.owner, title="Other", abstract="x", authors="x", subject="Mathematics")
        revision = Revision.objects.create(submission=other, version=1, file_hash="x")
        for n in range(3):
            p = other if n == 0 else Paper.objects.create(owner=self.owner, title=f"Other{n}", abstract="x", authors="x", subject="Mathematics")
            r = revision if n == 0 else Revision.objects.create(submission=p, version=1, file_hash="x")
            Assignment.objects.create(submission=p, reviewer=self.reviewers[0], revision=r, due_at=timezone.now()+timedelta(days=2))
        self.submit()
        self.assertFalse(Assignment.objects.filter(submission_id=self.pk, reviewer=self.reviewers[0]).exists())

    def test_two_acceptances_close_extra_invitation_and_prevent_third_acceptance(self):
        data = self.submit()
        invitations = list(Assignment.objects.filter(submission_id=self.pk).order_by("pk"))
        self.accept_two()
        third = invitations[2]
        self.assertEqual(self.action(auth_client(third.reviewer), "accept", expertise_confirmed=True, no_conflict=True, confidential=True).status_code, 403)
        self.assertEqual(Assignment.objects.filter(submission_id=self.pk, active=True).count(), 2)
        self.assertEqual(auth_client(third.reviewer).get("/api"+data["revisions"][0]["file_path"]).status_code, 404)
        first = Assignment.objects.get(pk=invitations[0].pk)
        self.assertTrue(abs((first.due_at-timezone.now()).total_seconds()-7*86400)<10)

    def test_publication_requires_two_approvals_and_records_scope(self):
        self.submit(); users = self.accept_two()
        self.assertEqual(self.action(auth_client(users[0]), "review", decision="approve").status_code, 200)
        self.assertEqual(self.action(self.m, "publish").status_code, 400)
        self.assertEqual(self.action(auth_client(users[1]), "review", decision="approve").status_code, 200)
        result = self.action(self.m, "publish")
        self.assertEqual(result.status_code, 200, result.content)
        public = Client().get(f'/api/explore/documents/{result.json()["document_id"]}/').json()["doc"]["research"]
        self.assertIn("2 independent", public["review_scope"])

    def test_conflicting_reviews_require_admin_changes_or_rejection(self):
        self.submit(); users = self.accept_two()
        self.action(auth_client(users[0]), "review", decision="approve")
        self.action(auth_client(users[1]), "review", decision="changes", note="Explain the sample selection.")
        self.assertEqual(Paper.objects.get(pk=self.pk).status, "approval")
        self.assertEqual(self.action(self.m, "publish").status_code, 400)
        self.assertEqual(self.action(self.m, "changes", note="Address the sample-selection concern.").status_code, 200)

    def test_independent_reviews_do_not_expose_other_review_feedback(self):
        self.submit(); users = self.accept_two()
        self.action(auth_client(users[0]), "review", decision="approve", note="Unique confidential first reviewer finding")
        data = auth_client(users[1]).get(f"/api/explore/research/{self.pk}/").json()
        self.assertEqual(len(data["assignments"]), 1)
        self.assertNotIn("Unique confidential first reviewer finding", str(data))
        author = self.a.get(f"/api/explore/research/{self.pk}/").json()
        self.assertIn("Unique confidential first reviewer finding", str(author["review_history"]))

    def test_decline_triggers_replacement_without_reinviting_decliner(self):
        self.submit()
        assignment = Assignment.objects.filter(submission_id=self.pk, active=True).first()
        result = self.action(auth_client(assignment.reviewer), "decline")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Assignment.objects.filter(submission_id=self.pk, active=True).count(), 3)
        self.assertEqual(Assignment.objects.filter(submission_id=self.pk, reviewer=assignment.reviewer).count(), 1)

    def test_reminder_sweep_is_idempotent_and_sms_stays_off(self):
        self.submit()
        Assignment.objects.filter(submission_id=self.pk).update(due_at=timezone.now()+timedelta(hours=12))
        with patch("notifications.services.notify") as notify, self.captureOnCommitCallbacks(execute=True):
            first = sweep_research_reviews(); second = sweep_research_reviews()
        self.assertEqual(first["reminded"], 3); self.assertEqual(second["reminded"], 0)
        self.assertEqual(notify.call_count, 3)
        from notifications.policy import for_verb
        self.assertEqual(for_verb("research.reminder")["sms"], "off")

    def test_expired_accepted_review_loses_file_access_and_gets_replacement(self):
        data = self.submit(); users = self.accept_two()
        assignment = Assignment.objects.get(submission_id=self.pk, reviewer=users[0], active=True)
        Assignment.objects.filter(pk=assignment.pk).update(due_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(auth_client(users[0]).get("/api"+data["revisions"][0]["file_path"]).status_code, 403)
        self.assertEqual(self.action(auth_client(users[0]), "review", decision="approve").status_code, 400)
        result = sweep_research_reviews()
        self.assertEqual(result["expired"], 1)
        self.assertGreaterEqual(Assignment.objects.filter(submission_id=self.pk, active=True, status="invited").count(), 1)
        self.assertEqual(auth_client(users[0]).get("/api"+data["revisions"][0]["file_path"]).status_code, 404)

    def test_no_matching_reviewer_alert_is_not_repeated_each_sweep(self):
        Reviewer.objects.update(approved=False)
        self.submit()
        self.assertTrue(Paper.objects.get(pk=self.pk).matching_notice_sent)
        with patch("notifications.services.notify") as notify, self.captureOnCommitCallbacks(execute=True):
            sweep_research_reviews(); sweep_research_reviews()
        self.assertEqual(notify.call_count, 0)

    def test_newly_approved_reviewer_is_matched_on_next_sweep(self):
        Reviewer.objects.update(approved=False); self.submit()
        Reviewer.objects.filter(user=self.reviewers[0]).update(approved=True)
        result = sweep_research_reviews()
        self.assertEqual(result["invited"], 1)

    def test_reviewer_availability_changes_only_future_matching(self):
        self.submit(); users = self.accept_two()
        result = auth_client(users[0]).patch("/api/explore/research/reviewers/", {"available": False}, content_type="application/json")
        self.assertEqual(result.status_code, 200); self.assertTrue(result.json()["approved"])
        self.assertEqual(self.action(auth_client(users[0]), "review", decision="approve").status_code, 200)

    def test_requirements_cannot_be_lowered_after_acceptance(self):
        self.submit(); self.accept_two()
        self.assertEqual(self.action(self.m, "configure", required_reviews=1).status_code, 400)

    def test_author_appeal_only_once_and_only_on_rejection(self):
        self.submit()
        self.assertEqual(self.action(self.a, "appeal", note="Please reconsider").status_code, 403)
        self.action(self.m, "reject", note="Outside scope")
        self.assertEqual(self.action(auth_client(self.reviewers[0]), "appeal", note="Please reconsider").status_code, 403)
        self.assertEqual(self.action(self.a, "appeal", note="The work is in scope; see introduction.").status_code, 200)
        self.assertEqual(self.action(self.a, "appeal", note="Again").status_code, 400)
        self.assertEqual(self.m.get(f"/api/explore/research/{self.pk}/").json()["open_case"]["kind"], "appeal")

    def test_upheld_appeal_starts_fresh_review_round(self):
        self.submit(); original = self.approve_two()
        self.action(self.m, "reject", note="Editorial concern")
        self.action(self.a, "appeal", note="New explanation for the concern")
        result = self.action(self.m, "uphold_appeal", note="Fresh subject assessment is justified.")
        self.assertEqual(result.status_code, 200, result.content)
        self.assertTrue(Case.objects.filter(submission_id=self.pk, kind="appeal", status="upheld").exists())
        self.assertFalse(Assignment.objects.filter(submission_id=self.pk, active=True, reviewer__in=original).exists())
        self.assertEqual(self.action(self.m, "publish").status_code, 400)
        self.assertEqual(self.action(self.m, "assign", reviewer_id=original[0].pk).status_code, 400)

    def test_dismissed_appeal_retains_rejection_and_reason(self):
        self.submit(); self.action(self.m, "reject", note="Insufficient evidence")
        self.action(self.a, "appeal", note="Please reconsider")
        result = self.action(self.m, "dismiss_appeal", note="Evidence remains insufficient.")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Paper.objects.get(pk=self.pk).status, "rejected")
        self.assertIn("Evidence remains insufficient", str(result.json()["cases"]))

    def test_correction_keeps_published_original_until_two_new_approvals(self):
        self.submit(); original = self.publish(); doc = Document.objects.get(pk=original["document_id"])
        old_name, old_hash = doc.file.name, doc.file_hash
        self.correction()
        doc.refresh_from_db()
        self.assertEqual(doc.file.name, old_name); self.assertEqual(doc.file_hash, old_hash)
        public = Client().get(f"/api/explore/documents/{doc.pk}/").json()["doc"]["research"]
        self.assertEqual(public["version"], 1); self.assertEqual(public["corrections"], [])
        self.assertEqual(self.action(self.m, "publish", public_note="Corrected result").status_code, 400)
        self.approve_two()
        self.assertEqual(self.action(self.m, "publish").status_code, 400)
        with self.captureOnCommitCallbacks(execute=True), patch("notifications.services.notify"):
            result = self.action(self.m, "publish", public_note="Corrected a numerical result in section two.")
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["document_id"], doc.pk)
        doc.refresh_from_db(); self.assertNotEqual(doc.file.name, old_name)
        self.assertFalse(doc.file.storage.exists(old_name))
        public = Client().get(f"/api/explore/documents/{doc.pk}/").json()["doc"]["research"]
        self.assertEqual(public["version"], 2)
        self.assertIn("Corrected a numerical", public["corrections"][0]["public_note"])

    def test_rejected_correction_preserves_public_paper_and_history(self):
        self.submit(); original = self.publish(); self.correction()
        result = self.action(self.m, "reject", note="The proposed change is unsupported.")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "published")
        self.assertEqual(result.json()["published_version"], 1)
        self.assertEqual(Client().get(f'/api/explore/documents/{original["document_id"]}/').status_code, 200)
        self.assertTrue(Case.objects.filter(submission_id=self.pk, kind="correction", status="rejected").exists())

    def test_author_can_cancel_correction_but_cannot_withdraw_public_original(self):
        self.submit(); self.publish(); self.correction()
        self.assertEqual(self.action(self.a, "withdraw", note="Cancel correction").status_code, 403)
        result = self.action(self.a, "cancel_correction", note="Withdraw this proposed correction only.")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "published")
        self.assertEqual(result.json()["published_version"], 1)
        self.assertFalse(Assignment.objects.filter(submission_id=self.pk, active=True).exists())

    def test_correction_revision_still_needs_fresh_reviews(self):
        self.submit(); self.publish(); self.correction(); self.approve_two()
        self.action(self.m, "changes", note="Clarify the correction.")
        result = self.a.post(f"/api/explore/research/{self.pk}/revisions/", {"file": self.pdf(b"correction v3"), "response": "Clarified the correction", "permission_confirmed": "true"})
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()["current_version"], 3)
        self.assertEqual(Case.objects.get(submission_id=self.pk, status="open").version, 3)
        self.assertEqual(self.action(self.m, "publish", public_note="Third version").status_code, 400)

    def test_sweep_task_is_registered_in_beat(self):
        from django.conf import settings
        self.assertEqual(settings.CELERY_BEAT_SCHEDULE["research-review-deadlines"]["task"], "documents.sweep_research_reviews")
        self.assertLessEqual(settings.CELERY_BEAT_SCHEDULE["research-review-deadlines"]["schedule"], 300)

    def test_revoked_reviewer_is_replaced_by_sweep(self):
        self.submit(); users = self.accept_two()
        Reviewer.objects.filter(user=users[0]).update(approved=False)
        outcome = sweep_research_reviews()
        self.assertEqual(outcome["expired"], 1)
        self.assertFalse(Assignment.objects.filter(submission_id=self.pk, reviewer=users[0], active=True).exists())
        self.assertGreater(outcome["invited"], 0)

    def test_correction_cannot_silently_restore_moderated_publication(self):
        self.submit(); original = self.publish(); self.correction(); self.approve_two()
        Document.objects.filter(pk=original["document_id"]).update(is_removed=True)
        self.assertEqual(self.action(self.m, "publish", public_note="Correction note").status_code, 400)
        self.assertTrue(Document.objects.get(pk=original["document_id"]).is_removed)

    def test_legacy_model_defaults_preserve_single_manual_review(self):
        paper = Paper.objects.create(owner=self.owner, title="Legacy paper", abstract="x", authors="x", subject="Mathematics")
        self.assertEqual(paper.required_reviews, 1)
        self.assertFalse(paper.automatic_matching)
