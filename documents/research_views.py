"""Private, revision-bound research review. No manuscript URLs from public storage."""
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q, Prefetch
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import (ResearchSubmission as Paper, ResearchReviewer as Reviewer,
                     ResearchRevision as Revision, ResearchAssignment as Assignment,
                     ResearchEvent as Event, Document, DocumentCategory, compute_file_hash)
from .research_serializers import SubmissionSerializer, ManuscriptSerializer, ReviewerApplicationSerializer, ActionSerializer


def is_research_admin(user):
    return bool(user.is_authenticated and (user.is_superuser or user.has_role("ADMIN")))


def require_admin(user):
    if not is_research_admin(user):
        raise PermissionDenied("Research administrator access is required.")


def check_uploader(user):
    from .views import _ban_error
    error = _ban_error(user)
    if error is not None:
        raise PermissionDenied(error.data["detail"])


def reviewer_active(user):
    return user.is_active and Reviewer.objects.filter(user=user, approved=True, available=True).exists()


def visible_papers(user):
    rows = Paper.objects.select_related("owner", "document").prefetch_related(
        Prefetch("assignments", queryset=Assignment.objects.filter(active=True).select_related("reviewer", "revision"), to_attr="active_assignments"))
    if is_research_admin(user):
        return rows
    return rows.filter(Q(owner=user) | Q(assignments__reviewer=user, assignments__active=True,
                                       assignments__reviewer__research_reviewer__approved=True)).distinct()


def event(paper, actor, action, note=""):
    Event.objects.create(submission=paper, actor=actor, action=action, note=note, version=paper.current_version)


def alert(user, paper, title, note="", *, admin=False):
    from django.conf import settings
    from notifications.services import notify
    # Absolute links also work when the recipient opens an email from outside the site.
    base = getattr(settings, "RESEARCH_ADMIN_URL", "https://admin.shikshacom.com") if admin else getattr(
        settings, "RESEARCH_PUBLIC_URL", "https://shikshacom.com")
    path = "/research" if admin else "/explore/research"
    url = f"{base.rstrip('/')}{path}?paper={paper.pk}"
    body = f"{note or 'Open Research Hub to see the submission status and next step.'}\n\n{url}"
    transaction.on_commit(lambda: notify(recipient=user, verb="research.updated", title=title,
        body=body, link_url=url, payload={"submission_id": paper.pk}))


def admin_alert(paper, title):
    # Staff queue owners, not every registered teacher or site user.
    for user in get_user_model().objects.filter(
            Q(is_superuser=True) | Q(user_roles__role__name="ADMIN", user_roles__is_active=True),
            is_active=True).distinct().iterator():
        if user.pk != paper.owner_id:
            alert(user, paper, title, admin=True)


def reviewer_data(profile, admin=False):
    data = {"id": profile.pk, "user_id": profile.user_id, "name": profile.user.get_full_name() or profile.user.username,
            "expertise": profile.expertise, "subjects": profile.subjects, "topics": profile.topics,
            "approved": profile.approved, "available": profile.available}
    if admin:
        data["email"] = profile.user.email
    return data


def paper_data(paper, user, detail=False):
    cached = getattr(paper, "active_assignments", None)
    assignment = (cached[0] if cached else None) if cached is not None else paper.assignments.filter(active=True).select_related("reviewer", "revision").first()
    admin = is_research_admin(user)
    full_access = admin or paper.owner_id == user.pk or bool(assignment and assignment.reviewer_id == user.pk
        and assignment.accepted_at and Reviewer.objects.filter(user=user, approved=True).exists())
    data = {"id": paper.pk, "title": paper.title, "abstract": paper.abstract, "authors": paper.authors,
        "institution": paper.institution, "subject": paper.subject, "topic": paper.topic,
        "original_url": paper.original_url, "status": paper.status, "status_label": paper.get_status_display(),
        "current_version": paper.current_version, "created_at": paper.created_at,
        "updated_at": paper.updated_at, "is_owner": paper.owner_id == user.pk,
        "document_id": paper.document_id if paper.document and not paper.document.is_removed else None,
        "assignment": None if not assignment else {"id": assignment.pk,
            "reviewer_id": assignment.reviewer_id, "reviewer_name": assignment.reviewer.get_full_name() or assignment.reviewer.username,
            "is_mine": assignment.reviewer_id == user.pk, "status": assignment.status,
            "version": assignment.revision.version, "due_at": assignment.due_at,
            "overdue": assignment.due_at < timezone.now() and assignment.status in ("invited", "accepted"),
            "decision": assignment.decision, "checks": assignment.checks}}
    if detail and full_access:
        data["revisions"] = [{"id": r.pk, "version": r.version, "response": r.response,
            "created_at": r.created_at, "file_path": f"/explore/research/{paper.pk}/revisions/{r.pk}/file/"}
            for r in paper.revisions.all()]
        data["events"] = list(paper.events.values("action", "note", "version", "created_at"))
    return data


class ResearchListView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "documents_upload"

    def get_throttles(self):
        return [ScopedRateThrottle()] if self.request.method == "POST" else []

    def get(self, request):
        rows = visible_papers(request.user)
        scope = request.query_params.get("scope", "mine")
        if scope == "admin":
            require_admin(request.user)
        elif scope == "reviews":
            rows = rows.filter(assignments__reviewer=request.user, assignments__active=True).distinct()
        else:
            rows = rows.filter(owner=request.user)
        state = request.query_params.get("status")
        if state:
            rows = rows.filter(status=state)
        query = request.query_params.get("q", "").strip()[:300]
        if query:
            rows = rows.filter(Q(title__icontains=query) | Q(subject__icontains=query) | Q(authors__icontains=query))
        from .views import _int_param
        page = _int_param(request, "page", 1, 100000)
        count = rows.count()
        return Response({"results": [paper_data(p, request.user) for p in rows[(page-1)*20:page*20]], "count": count,
                         "page": page, "is_admin": is_research_admin(request.user)})

    @transaction.atomic
    def post(self, request):
        check_uploader(request.user)
        serializer = SubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        upload = data.pop("file")
        data.pop("permission_confirmed")
        response = data.pop("response")
        digest = compute_file_hash(upload)
        # Serialize duplicate submissions by the same account on PostgreSQL.
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        if Revision.objects.filter(submission__owner=request.user, file_hash=digest,
                submission__status__in=[Paper.Status.SUBMITTED, Paper.Status.INVITED, Paper.Status.REVIEWING,
                                       Paper.Status.CHANGES, Paper.Status.APPROVAL, Paper.Status.PUBLISHED]).exists():
            raise ValidationError("You have already submitted this file. Open your existing submission.")
        paper = Paper.objects.create(owner=request.user, **data)
        Revision.objects.create(submission=paper, version=1, file=upload, file_hash=digest, response=response)
        event(paper, request.user, "submitted")
        admin_alert(paper, "Research paper awaiting reviewer assignment")
        alert(request.user, paper, "Your research paper was submitted")
        return Response(paper_data(paper, request.user, True), status=201)


class ResearchDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, paper_id):
        paper = get_object_or_404(visible_papers(request.user), pk=paper_id)
        return Response(paper_data(paper, request.user, True))

    @transaction.atomic
    def post(self, request, paper_id):
        # Lock the parent before changing assignments/revisions/publication. Unique constraint is a backstop.
        paper = get_object_or_404(Paper.objects.select_for_update(), pk=paper_id)
        if not visible_papers(request.user).filter(pk=paper.pk).exists():
            raise PermissionDenied()
        check_uploader(request.user)
        serializer = ActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data["version"] != paper.current_version:
            raise ValidationError("This submission has a newer revision. Refresh before taking action.")
        action, note = data["action"], data["note"]
        admin = is_research_admin(request.user)
        assignment = paper.assignments.filter(active=True).select_related("revision").first()
        revision = paper.revisions.get(version=paper.current_version)
        terminal = paper.status in (Paper.Status.PUBLISHED, Paper.Status.WITHDRAWN, Paper.Status.REJECTED)
        if action == "assign":
            require_admin(request.user)
            if terminal or paper.status not in (Paper.Status.SUBMITTED, Paper.Status.INVITED, Paper.Status.REVIEWING):
                raise ValidationError("This paper is not awaiting a reviewer. Request changes or finish the current decision first.")
            reviewer = get_object_or_404(Reviewer.objects.select_related("user"), user_id=data.get("reviewer_id"), approved=True, available=True, user__is_active=True)
            if reviewer.user_id in (paper.owner_id, request.user.pk):
                raise ValidationError("Assign an independent reviewer, different from the author and assigning administrator.")
            if paper.subject.casefold() not in {s.casefold() for s in reviewer.subjects}:
                raise ValidationError("The reviewer must have approved expertise in this paper’s main subject.")
            if assignment:
                assignment.active, assignment.status = False, "reassigned"
                assignment.save(update_fields=["active", "status"])
                alert(assignment.reviewer, paper, "Research review assignment ended")
            Assignment.objects.create(submission=paper, reviewer=reviewer.user, revision=revision,
                                      due_at=timezone.now()+timedelta(days=data["days"]))
            paper.status = Paper.Status.INVITED
            alert(reviewer.user, paper, "You are invited to review a research paper", f"{paper.title}\nSubject: {paper.subject}. Accept or decline in Research Hub.")
        elif action in ("accept", "decline", "review"):
            if not assignment or assignment.reviewer_id != request.user.pk or not reviewer_active(request.user):
                raise PermissionDenied("This review is not assigned to you or your reviewer access is inactive.")
            if assignment.revision_id != revision.pk:
                raise ValidationError("The assignment belongs to an older revision.")
            if action in ("accept", "decline"):
                if assignment.status != "invited" or paper.status != Paper.Status.INVITED:
                    raise ValidationError("This invitation is no longer available.")
                if action == "accept":
                    if not all(data[k] for k in ("expertise_confirmed", "no_conflict", "confidential")):
                        raise ValidationError("Confirm relevant expertise, no conflict of interest, and confidentiality.")
                    assignment.status, assignment.accepted_at = "accepted", timezone.now()
                    paper.status = Paper.Status.REVIEWING
                else:
                    assignment.status, assignment.active = "declined", False
                    paper.status = Paper.Status.SUBMITTED
                    admin_alert(paper, "Reviewer declined; assignment needed")
            else:
                if assignment.status != "accepted" or paper.status != Paper.Status.REVIEWING:
                    raise ValidationError("Accept the current assignment before submitting a review.")
                if not data.get("decision") or not note:
                    raise ValidationError("Choose a decision and explain your review findings.")
                checks = data.get("checks", {})
                if set(checks) != {"relevance", "clarity", "methods", "evidence", "references"}:
                    raise ValidationError("Complete each item in the review checklist.")
                if data["decision"] == "approve" and ("issues" in checks.values() or checks["relevance"] != "checked"):
                    raise ValidationError("Resolve checklist issues before recommending approval.")
                assignment.checks = checks
                assignment.decision, assignment.feedback, assignment.status = data["decision"], note, "completed"
                if data["decision"] == "changes":
                    paper.status, assignment.active = Paper.Status.CHANGES, False
                else:
                    paper.status = Paper.Status.APPROVAL
                admin_alert(paper, "Research review completed")
            assignment.save()
        elif action in ("changes", "reject", "publish"):
            require_admin(request.user)
            if terminal:
                raise ValidationError("This submission is already closed.")
            if action == "publish":
                if paper.owner_id == request.user.pk:
                    raise PermissionDenied("Another administrator must publish your own submission.")
                if paper.status != Paper.Status.APPROVAL or not assignment or assignment.status != "completed" or assignment.decision != "approve" or assignment.revision_id != revision.pk:
                    raise ValidationError("Publication requires a completed approval review of the current revision.")
                if not Reviewer.objects.filter(user_id=assignment.reviewer_id, approved=True, user__is_active=True).exists():
                    raise ValidationError("The reviewer’s approval is no longer active. Request changes and arrange a new review.")
                category, _ = DocumentCategory.objects.get_or_create(slug="research-papers", defaults={"name": "Research Papers"})
                if Document.objects.filter(owner=paper.owner, file_hash=revision.file_hash, is_removed=False).exists():
                    raise ValidationError("This file already exists in the author’s public library. Resolve that duplicate before publishing.")
                doc = Document(owner=paper.owner, title=paper.title, description=paper.abstract, subject=paper.subject,
                    topic=paper.topic, institution=paper.institution, category=category, filetype="PDF", file_hash=revision.file_hash)
                with revision.file.open("rb") as content:
                    doc.file.save(f"research-{paper.pk}-v{revision.version}.pdf", content, save=False)
                doc.save()
                paper.document, paper.status = doc, Paper.Status.PUBLISHED
            else:
                if not note:
                    raise ValidationError("Explain the requested changes or reason for rejection.")
                paper.status = Paper.Status.CHANGES if action == "changes" else Paper.Status.REJECTED
            if assignment:
                assignment.active = False
                assignment.save(update_fields=["active"])
        elif action == "withdraw":
            if not admin and paper.owner_id != request.user.pk:
                raise PermissionDenied()
            if paper.status in (Paper.Status.WITHDRAWN, Paper.Status.REJECTED):
                raise ValidationError("This submission is already closed.")
            if paper.status == Paper.Status.PUBLISHED and not admin:
                raise PermissionDenied("Contact an administrator to withdraw a published paper.")
            if not note:
                raise ValidationError("Explain why the paper is being withdrawn.")
            if paper.document_id:
                paper.document.is_removed, paper.document.removed_at = True, timezone.now()
                paper.document.save(update_fields=["is_removed", "removed_at"])
                # Remove the public copy (storage.delete also purges Bunny CDN).
                # The immutable private revision remains in the review record.
                storage, name = paper.document.file.storage, paper.document.file.name
                if name:
                    transaction.on_commit(lambda: storage.delete(name))
            paper.status = Paper.Status.WITHDRAWN
            if assignment:
                assignment.active = False
                assignment.save(update_fields=["active"])
        paper.save()
        event(paper, request.user, f"review_{data["decision"]}" if action == "review" else action, note)
        alert(paper.owner, paper, f"Research paper: {paper.get_status_display()}", note)
        return Response(paper_data(paper, request.user, True))


class ResearchRevisionView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "documents_upload"
    throttle_classes = [ScopedRateThrottle]

    @transaction.atomic
    def post(self, request, paper_id):
        check_uploader(request.user)
        paper = get_object_or_404(Paper.objects.select_for_update(), pk=paper_id, owner=request.user)
        if paper.status != Paper.Status.CHANGES:
            raise ValidationError("A new revision is allowed only after changes have been requested.")
        serializer = ManuscriptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not data["response"]:
            raise ValidationError("Explain how this revision addresses the requested changes.")
        digest = compute_file_hash(data["file"])
        if paper.revisions.filter(file_hash=digest).exists():
            raise ValidationError("This file matches an earlier revision. Attach the revised PDF.")
        paper.current_version += 1
        Revision.objects.create(submission=paper, version=paper.current_version, file=data["file"],
                                file_hash=digest, response=data["response"])
        paper.assignments.filter(active=True).update(active=False, status="superseded")
        paper.status = Paper.Status.SUBMITTED
        paper.save()
        event(paper, request.user, "revised", data["response"])
        admin_alert(paper, "Revised research paper awaiting assignment")
        return Response(paper_data(paper, request.user, True), status=201)


class ResearchFileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, paper_id, revision_id):
        paper = get_object_or_404(visible_papers(request.user), pk=paper_id)
        if not is_research_admin(request.user) and paper.owner_id != request.user.pk:
            if not paper.assignments.filter(reviewer=request.user, active=True, accepted_at__isnull=False,
                    reviewer__research_reviewer__approved=True).exists():
                raise PermissionDenied("Accept the review invitation before opening the manuscript.")
        revision = get_object_or_404(paper.revisions, pk=revision_id)
        try:
            response = FileResponse(revision.file.open("rb"), as_attachment=True,
                                    filename=f"research-{paper.pk}-v{revision.version}.pdf", content_type="application/pdf")
        except FileNotFoundError:
            raise ValidationError("The manuscript file is unavailable. Contact an administrator.")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class ResearchReviewersView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.query_params.get("scope") == "admin":
            require_admin(request.user)
            return Response({"results": [reviewer_data(p, True) for p in Reviewer.objects.select_related("user").order_by("-updated_at")]})
        profile = Reviewer.objects.filter(user=request.user).select_related("user").first()
        return Response({"profile": reviewer_data(profile) if profile else None, "is_admin": is_research_admin(request.user)})

    @transaction.atomic
    def post(self, request):
        check_uploader(request.user)
        serializer = ReviewerApplicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile, _ = Reviewer.objects.update_or_create(user=request.user,
            defaults={**serializer.validated_data, "approved": False, "approved_by": None})
        return Response(reviewer_data(profile), status=201)


class ResearchReviewerAdminView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def patch(self, request, reviewer_id):
        require_admin(request.user)
        profile = get_object_or_404(Reviewer.objects.select_for_update(), pk=reviewer_id)
        if profile.user_id == request.user.pk:
            raise PermissionDenied("Another administrator must approve your reviewer application.")
        class Decision(serializers.Serializer):
            approved = serializers.BooleanField()
        serializer = Decision(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile.approved = serializer.validated_data["approved"]
        profile.approved_by = request.user if profile.approved else None
        profile.save()
        # Do not leave private files accessible to a suspended reviewer.
        if not profile.approved:
            for assignment in Assignment.objects.select_related("submission").filter(reviewer=profile.user, active=True):
                paper = Paper.objects.select_for_update().get(pk=assignment.submission_id)
                assignment.active, assignment.status = False, "revoked"
                assignment.save()
                if paper.status in (Paper.Status.INVITED, Paper.Status.REVIEWING, Paper.Status.APPROVAL):
                    paper.status = Paper.Status.SUBMITTED
                    paper.save()
                    event(paper, request.user, "reviewer_revoked")
        return Response(reviewer_data(profile, True))
