"""Private revision-bound subject review, invitations, appeals and corrections."""
from datetime import timedelta
from django.conf import settings
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
                     ResearchCase as Case, Document, DocumentCategory, compute_file_hash)
from .research_serializers import SubmissionSerializer, ManuscriptSerializer, ReviewerApplicationSerializer, ActionSerializer
from .research_workflow import (is_research_admin, event, alert, admin_alert, current_assignments,
    close_assignments, refresh_stage, fill_review_slots, invite, LIVE_STATES, remove_public_copy)


def require_admin(user):
    if not is_research_admin(user):
        raise PermissionDenied("Research administrator access is required.")


def check_uploader(user):
    from .views import _ban_error
    error = _ban_error(user)
    if error is not None:
        raise PermissionDenied(error.data["detail"])


def reviewer_active(user):
    return user.is_active and Reviewer.objects.filter(user=user, approved=True).exists()


def visible_papers(user):
    rows = Paper.objects.select_related("owner", "document").prefetch_related(Prefetch("assignments",
        queryset=Assignment.objects.filter(active=True).select_related("reviewer", "revision"), to_attr="active_assignments"), "cases")
    if is_research_admin(user):
        return rows
    return rows.filter(Q(owner=user) | Q(assignments__reviewer=user, assignments__active=True,
        assignments__reviewer__research_reviewer__approved=True)).distinct()


def reviewer_data(profile, admin=False):
    data = {"id": profile.pk, "user_id": profile.user_id,
        "name": profile.user.get_full_name() or profile.user.username,
        "expertise": profile.expertise, "subjects": profile.subjects, "topics": profile.topics,
        "approved": profile.approved, "available": profile.available}
    if admin:
        data["email"] = profile.user.email
    return data


def assignment_data(assignment, user):
    return {"id": assignment.pk, "reviewer_id": assignment.reviewer_id,
        "reviewer_name": assignment.reviewer.get_full_name() or assignment.reviewer.username,
        "is_mine": assignment.reviewer_id == user.pk, "status": assignment.status,
        "version": assignment.revision.version, "due_at": assignment.due_at,
        "overdue": assignment.due_at <= timezone.now() and assignment.status in ("invited", "accepted"),
        "decision": assignment.decision, "checks": assignment.checks, "feedback": assignment.feedback}


def paper_data(paper, user, detail=False):
    cached = getattr(paper, "active_assignments", None)
    assignments = cached if cached is not None else list(current_assignments(paper).select_related("reviewer", "revision"))
    assignments = [a for a in assignments if a.revision.version == paper.current_version]
    admin = is_research_admin(user)
    owner = paper.owner_id == user.pk
    own = next((a for a in assignments if a.reviewer_id == user.pk), None)
    readable = bool(own and own.accepted_at and (own.status == "completed" or own.due_at > timezone.now()) and reviewer_active(user))
    shown = assignments if admin or owner else ([own] if own else [])
    data = {"id": paper.pk, "title": paper.title, "abstract": paper.abstract, "authors": paper.authors,
        "institution": paper.institution, "subject": paper.subject, "topic": paper.topic,
        "original_url": paper.original_url, "status": paper.status, "status_label": paper.get_status_display(),
        "current_version": paper.current_version, "published_version": paper.published_version,
        "published_review_count": paper.published_review_count,
        "required_reviews": paper.required_reviews, "automatic_matching": paper.automatic_matching,
        "created_at": paper.created_at, "updated_at": paper.updated_at, "is_owner": owner,
        "document_id": paper.document_id if paper.document and not paper.document.is_removed else None,
        "assignment": assignment_data(own or (shown[0] if shown else None), user) if shown else None,
        "assignments": [assignment_data(a, user) for a in shown],
        "review_progress": {"required": paper.required_reviews,
            "completed": sum(a.status == "completed" for a in assignments),
            "accepted": sum(a.status == "accepted" for a in assignments),
            "invited": sum(a.status == "invited" for a in assignments)}}
    if detail and (admin or owner or readable):
        data["revisions"] = [{"id": r.pk, "version": r.version, "response": r.response,
            "created_at": r.created_at, "file_path": f"/explore/research/{paper.pk}/revisions/{r.pk}/file/"}
            for r in paper.revisions.all() if admin or owner or r.version <= own.revision.version]
        events = paper.events.all()
        if not (admin or owner):
            events = events.filter(Q(actor=user) | ~Q(action__startswith="review_"))
        data["events"] = list(events.values("action", "note", "version", "created_at"))
    if admin or owner:
        open_case = next((case for case in paper.cases.all() if case.status == "open"), None)
        data["open_case"] = {"kind": open_case.kind, "version": open_case.version} if open_case else None
    if detail and (admin or owner):
        data["review_history"] = [assignment_data(a, user) for a in paper.assignments.exclude(decision="").select_related("reviewer", "revision")]
        data["cases"] = list(paper.cases.values("id", "kind", "version", "reason", "status", "resolution", "public_note", "created_at", "decided_at"))
    return data


def reset_round(paper):
    close_assignments(paper, "superseded")
    paper.status, paper.matching_notice_sent = Paper.Status.SUBMITTED, False
    paper.save()
    fill_review_slots(paper)


def publish(paper, user, data):
    if paper.owner_id == user.pk:
        raise PermissionDenied("Another administrator must publish your own submission.")
    reviews = list(current_assignments(paper).select_related("reviewer"))
    completed = [a for a in reviews if a.status == "completed"]
    if paper.status != Paper.Status.APPROVAL or len(completed) < paper.required_reviews or any(a.decision != "approve" for a in completed):
        raise ValidationError(f"Publication requires approval from {paper.required_reviews} independent reviewers on the current revision, with no unresolved review objections.")
    if len({a.reviewer_id for a in completed}) < paper.required_reviews:
        raise ValidationError("Independent reviewers are required.")
    if any(not reviewer_active(a.reviewer) for a in completed):
        raise ValidationError("A reviewer’s approval is no longer active. Arrange a replacement review.")
    if user.pk in {a.reviewer_id for a in completed}:
        raise PermissionDenied("The final publisher must be different from the reviewers.")
    correction = paper.cases.filter(kind="correction", status="open").first()
    if paper.document_id and paper.document.is_removed:
        raise ValidationError("The existing publication is hidden by moderation. Resolve that before publishing a correction.")
    if correction and not data["public_note"]:
        raise ValidationError("Write a public correction notice before publishing the corrected version.")
    revision = paper.revisions.get(version=paper.current_version)
    duplicates = Document.objects.filter(owner=paper.owner, file_hash=revision.file_hash, is_removed=False)
    if paper.document_id:
        duplicates = duplicates.exclude(pk=paper.document_id)
    if duplicates.exists():
        raise ValidationError("This file already exists in the author’s public library. Resolve that duplicate before publishing.")
    category, _ = DocumentCategory.objects.get_or_create(slug="research-papers", defaults={"name": "Research Papers"})
    doc = paper.document or Document(owner=paper.owner)
    old_storage, old_name = doc.file.storage, doc.file.name if doc.file else ""
    doc.title, doc.description, doc.subject, doc.topic = paper.title, paper.abstract, paper.subject, paper.topic
    doc.institution, doc.category, doc.filetype, doc.file_hash = paper.institution, category, "PDF", revision.file_hash
    doc.is_removed, doc.removed_at = False, None
    with revision.file.open("rb") as content:
        doc.file.save(f"research-{paper.pk}-v{revision.version}.pdf", content, save=False)
    doc.save()
    if old_name and old_name != doc.file.name:
        transaction.on_commit(lambda: remove_public_copy(old_storage, old_name))
    paper.document, paper.status = doc, Paper.Status.PUBLISHED
    paper.published_version, paper.published_review_count = paper.current_version, len(completed)
    if correction:
        correction.status, correction.public_note = "published", data["public_note"]
        correction.resolution, correction.decided_by, correction.decided_at = data["note"], user, timezone.now()
        correction.save()
    close_assignments(paper)


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
        if request.query_params.get("status"):
            rows = rows.filter(status=request.query_params["status"])
        if request.query_params.get("case") in ("appeal", "correction"):
            rows = rows.filter(cases__kind=request.query_params["case"], cases__status="open").distinct()
        query = request.query_params.get("q", "").strip()[:300]
        if query:
            rows = rows.filter(Q(title__icontains=query) | Q(subject__icontains=query) | Q(authors__icontains=query))
        from .views import _int_param
        page = _int_param(request, "page", 1, 100000)
        return Response({"results": [paper_data(p, request.user) for p in rows[(page-1)*20:page*20]],
            "count": rows.count(), "page": page, "is_admin": is_research_admin(request.user)})

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
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        if Revision.objects.filter(submission__owner=request.user, file_hash=digest,
                submission__status__in=[*LIVE_STATES, Paper.Status.CHANGES, Paper.Status.APPROVAL, Paper.Status.PUBLISHED]).exists():
            raise ValidationError("You have already submitted this file. Open your existing submission.")
        paper = Paper.objects.create(owner=request.user, required_reviews=settings.RESEARCH_DEFAULT_REVIEWS,
                                    automatic_matching=settings.RESEARCH_AUTO_MATCHING, **data)
        Revision.objects.create(submission=paper, version=1, file=upload, file_hash=digest, response=response)
        event(paper, request.user, "submitted")
        fill_review_slots(paper)
        if not paper.matching_notice_sent:
            admin_alert(paper, "Research paper submitted")
        alert(request.user, paper, "Your research paper was submitted")
        return Response(paper_data(paper, request.user, True), status=201)


class ResearchDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, paper_id):
        return Response(paper_data(get_object_or_404(visible_papers(request.user), pk=paper_id), request.user, True))

    @transaction.atomic
    def post(self, request, paper_id):
        paper = get_object_or_404(Paper.objects.select_for_update(of=("self",)).select_related("document"), pk=paper_id)
        if not visible_papers(request.user).filter(pk=paper.pk).exists():
            raise PermissionDenied()
        check_uploader(request.user)
        serializer = ActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data["version"] != paper.current_version:
            raise ValidationError("This submission has a newer revision. Refresh before taking action.")
        action, note, admin = data["action"], data["note"], is_research_admin(request.user)
        notice_title = None
        revision = paper.revisions.get(version=paper.current_version)
        terminal = paper.status in (Paper.Status.PUBLISHED, Paper.Status.WITHDRAWN, Paper.Status.REJECTED)
        if action == "assign":
            require_admin(request.user)
            if paper.status not in (*LIVE_STATES, Paper.Status.APPROVAL):
                raise ValidationError("This paper is not awaiting a review.")
            reviewer = get_object_or_404(Reviewer.objects.select_related("user"), user_id=data.get("reviewer_id"), approved=True, available=True, user__is_active=True)
            if reviewer.user_id in (paper.owner_id, request.user.pk):
                raise ValidationError("Assign a reviewer different from the author and assigning administrator.")
            if paper.subject.casefold() not in {s.casefold() for s in reviewer.subjects}:
                raise ValidationError("The reviewer must have approved expertise in this paper’s main subject.")
            target = None
            if data.get("assignment_id"):
                target = get_object_or_404(current_assignments(paper), pk=data["assignment_id"])
            elif paper.required_reviews == 1:
                target = current_assignments(paper).first()
            if target:
                target.active, target.status = False, "reassigned"
                target.save(update_fields=["active", "status"])
                alert(target.reviewer, paper, "Research review assignment ended")
            occupied = current_assignments(paper).filter(status__in=["accepted", "completed"]).count()
            if occupied >= paper.required_reviews:
                raise ValidationError("All review slots are occupied. Select the assignment to replace.")
            if current_assignments(paper).filter(reviewer=reviewer.user).exists():
                raise ValidationError("This reviewer already has an active invitation or review.")
            if paper.cases.filter(kind="appeal", status="upheld", version=paper.current_version).exists() and paper.assignments.filter(reviewer=reviewer.user, revision=revision).exclude(decision="").exists():
                raise ValidationError("An upheld appeal needs a fresh reviewer, different from the original reviewers.")
            invite(paper, reviewer, revision, actor=request.user, days=data["days"])
            refresh_stage(paper)
        elif action in ("accept", "decline", "review"):
            assignment = current_assignments(paper).filter(reviewer=request.user).first()
            if not assignment or not reviewer_active(request.user):
                raise PermissionDenied("This review is not assigned to you or your reviewer access is inactive.")
            if assignment.due_at <= timezone.now() and assignment.status != "completed":
                raise ValidationError("This deadline has passed. The assignment will be reassigned.")
            if action in ("accept", "decline"):
                if assignment.status != "invited" or paper.status not in LIVE_STATES:
                    raise ValidationError("This invitation is no longer available.")
                if action == "accept":
                    if not all(data[k] for k in ("expertise_confirmed", "no_conflict", "confidential")):
                        raise ValidationError("Confirm relevant expertise, no conflict of interest, and confidentiality.")
                    occupied = current_assignments(paper).filter(status__in=["accepted", "completed"]).count()
                    if occupied >= paper.required_reviews:
                        raise ValidationError("All reviewer slots have already been filled.")
                    assignment.status, assignment.accepted_at = "accepted", timezone.now()
                    assignment.due_at, assignment.reminded_at = timezone.now()+timedelta(days=assignment.review_days), None
                    assignment.save()
                    if occupied+1 >= paper.required_reviews:
                        current_assignments(paper).filter(status="invited").update(active=False, status="filled")
                else:
                    assignment.status, assignment.active = "declined", False
                    assignment.save()
                    if not paper.automatic_matching:
                        admin_alert(paper, "Reviewer declined; assignment needed")
            else:
                if assignment.status != "accepted" or paper.status not in LIVE_STATES:
                    raise ValidationError("Accept the current assignment before submitting a review.")
                if not data.get("decision") or not note:
                    raise ValidationError("Choose a decision and explain your review findings.")
                checks = data.get("checks", {})
                if set(checks) != {"relevance", "clarity", "methods", "evidence", "references"}:
                    raise ValidationError("Complete each item in the review checklist.")
                if data["decision"] == "approve" and ("issues" in checks.values() or checks["relevance"] != "checked"):
                    raise ValidationError("Resolve checklist issues before recommending approval.")
                assignment.checks, assignment.decision, assignment.feedback, assignment.status = checks, data["decision"], note, "completed"
                assignment.save()
                if paper.required_reviews == 1 and data["decision"] == "changes":
                    paper.status = Paper.Status.CHANGES
                    assignment.active = False
                    assignment.save(update_fields=["active"])
                admin_alert(paper, "Research review completed")
            refresh_stage(paper)
            fill_review_slots(paper)
        elif action in ("changes", "reject", "publish"):
            require_admin(request.user)
            if terminal:
                raise ValidationError("This submission is already closed.")
            if action == "publish":
                publish(paper, request.user, data)
            else:
                if not note:
                    raise ValidationError("Explain the requested changes or reason for rejection.")
                correction = paper.cases.filter(kind="correction", status="open").first()
                if action == "reject" and correction:
                    correction.status, correction.resolution = "rejected", note
                    correction.decided_by, correction.decided_at = request.user, timezone.now()
                    correction.save()
                    paper.status = Paper.Status.PUBLISHED
                    notice_title = "Research correction rejected; previous publication retained"
                else:
                    paper.status = Paper.Status.CHANGES if action == "changes" else Paper.Status.REJECTED
                close_assignments(paper)
        elif action in ("match", "configure"):
            require_admin(request.user)
            if paper.status not in LIVE_STATES:
                raise ValidationError("Matching settings can change only while arranging reviews.")
            if action == "configure":
                if "required_reviews" in data and data["required_reviews"] != paper.required_reviews and current_assignments(paper).filter(status__in=["accepted", "completed"]).exists():
                    raise ValidationError("Review requirements cannot change after a reviewer accepts.")
                if "required_reviews" in data:
                    paper.required_reviews = data["required_reviews"]
                if "automatic_matching" in data:
                    paper.automatic_matching = data["automatic_matching"]
                paper.save()
            fill_review_slots(paper)
        elif action == "appeal":
            if paper.owner_id != request.user.pk or paper.status != Paper.Status.REJECTED:
                raise PermissionDenied("Only the author can appeal a rejected submission.")
            if not note:
                raise ValidationError("Explain why the decision should be reconsidered.")
            if paper.cases.filter(kind="appeal", version=paper.current_version).exists():
                raise ValidationError("An appeal already exists for this version.")
            Case.objects.create(submission=paper, kind="appeal", version=paper.current_version, reason=note)
            admin_alert(paper, "Research author appeal awaiting decision")
        elif action in ("uphold_appeal", "dismiss_appeal"):
            require_admin(request.user)
            if paper.owner_id == request.user.pk:
                raise PermissionDenied("Another administrator must decide your appeal.")
            case = get_object_or_404(paper.cases, kind="appeal", status="open", version=paper.current_version)
            if paper.status != Paper.Status.REJECTED or not note:
                raise ValidationError("Provide a reason for the appeal decision on a rejected paper.")
            case.status, case.resolution = ("upheld" if action == "uphold_appeal" else "dismissed"), note
            case.decided_by, case.decided_at = request.user, timezone.now()
            case.save()
            if action == "uphold_appeal":
                paper.automatic_matching = True
                reset_round(paper)
        elif action == "cancel_correction":
            if paper.owner_id != request.user.pk and not admin:
                raise PermissionDenied()
            case = get_object_or_404(paper.cases, kind="correction", status="open")
            if not note:
                raise ValidationError("Explain why the correction request is being cancelled.")
            case.status, case.resolution, case.decided_by, case.decided_at = "cancelled", note, request.user, timezone.now()
            case.save()
            close_assignments(paper)
            paper.status = Paper.Status.PUBLISHED
        elif action == "withdraw":
            if not admin and paper.owner_id != request.user.pk:
                raise PermissionDenied()
            if paper.status in (Paper.Status.WITHDRAWN, Paper.Status.REJECTED):
                raise ValidationError("This submission is already closed.")
            if paper.document_id and not admin:
                raise PermissionDenied("Contact an administrator to withdraw a published paper. You can cancel a pending correction separately.")
            if not note:
                raise ValidationError("Explain why the paper is being withdrawn.")
            if paper.document_id:
                paper.document.is_removed, paper.document.removed_at = True, timezone.now()
                paper.document.save(update_fields=["is_removed", "removed_at"])
                storage, name = paper.document.file.storage, paper.document.file.name
                if name:
                    transaction.on_commit(lambda: remove_public_copy(storage, name))
            paper.status = Paper.Status.WITHDRAWN
            close_assignments(paper)
            paper.cases.filter(status="open").update(status="closed", resolution=note, decided_by=request.user, decided_at=timezone.now())
        paper.save()
        event(paper, request.user, f"review_{data['decision']}" if action == "review" else action, note)
        titles = {"appeal": "Your research appeal was submitted", "uphold_appeal": "Research appeal upheld — fresh reviews requested",
                  "dismiss_appeal": "Research appeal dismissed", "cancel_correction": "Correction request cancelled; previous publication retained"}
        alert(paper.owner, paper, notice_title or titles.get(action) or f"Research paper: {paper.get_status_display()}", note)
        return Response(paper_data(paper, request.user, True))


class ResearchRevisionView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "documents_upload"
    throttle_classes = [ScopedRateThrottle]
    correction = False

    @transaction.atomic
    def post(self, request, paper_id):
        check_uploader(request.user)
        paper = get_object_or_404(Paper.objects.select_for_update(of=("self",)).select_related("document"), pk=paper_id, owner=request.user)
        if self.correction:
            if paper.status != Paper.Status.PUBLISHED or not paper.document_id or paper.document.is_removed:
                raise ValidationError("A correction can be proposed only for a visible published paper.")
        elif paper.status != Paper.Status.CHANGES:
            raise ValidationError("A new revision is allowed only after changes have been requested.")
        serializer = ManuscriptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not data["response"]:
            raise ValidationError("Explain the correction or how this revision addresses the requested changes.")
        digest = compute_file_hash(data["file"])
        if paper.revisions.filter(file_hash=digest).exists():
            raise ValidationError("This file matches an earlier revision. Attach the revised PDF.")
        paper.current_version += 1
        Revision.objects.create(submission=paper, version=paper.current_version, file=data["file"], file_hash=digest, response=data["response"])
        if self.correction:
            Case.objects.create(submission=paper, kind="correction", version=paper.current_version, reason=data["response"])
            paper.required_reviews = settings.RESEARCH_DEFAULT_REVIEWS
            paper.automatic_matching = settings.RESEARCH_AUTO_MATCHING
        else:
            paper.cases.filter(kind="correction", status="open").update(version=paper.current_version)
        reset_round(paper)
        event(paper, request.user, "correction_requested" if self.correction else "revised", data["response"])
        if not paper.matching_notice_sent:
            admin_alert(paper, "Research correction awaiting review" if self.correction else "Revised research paper awaiting review")
        return Response(paper_data(paper, request.user, True), status=201)


class ResearchCorrectionView(ResearchRevisionView):
    correction = True


class ResearchFileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, paper_id, revision_id):
        paper = get_object_or_404(visible_papers(request.user), pk=paper_id)
        revision = get_object_or_404(paper.revisions, pk=revision_id)
        if not is_research_admin(request.user) and paper.owner_id != request.user.pk:
            assignment = current_assignments(paper).filter(reviewer=request.user, accepted_at__isnull=False,
                reviewer__research_reviewer__approved=True).first()
            if not assignment or (assignment.status != "completed" and assignment.due_at <= timezone.now()) or revision.version > assignment.revision.version:
                raise PermissionDenied("Accept an unexpired review invitation before opening this manuscript.")
        try:
            response = FileResponse(revision.file.open("rb"), as_attachment=True,
                filename=f"research-{paper.pk}-v{revision.version}.pdf", content_type="application/pdf")
        except FileNotFoundError:
            raise ValidationError("The manuscript file is unavailable. Contact an administrator.")
        response["Cache-Control"], response["X-Content-Type-Options"] = "private, no-store", "nosniff"
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
        if Assignment.objects.filter(reviewer=request.user, active=True, status__in=["accepted", "completed"]).exists():
            raise ValidationError("Finish your current reviews or contact an administrator before changing approved expertise.")
        serializer = ReviewerApplicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile, _ = Reviewer.objects.update_or_create(user=request.user,
            defaults={**serializer.validated_data, "approved": False, "approved_by": None})
        return Response(reviewer_data(profile), status=201)

    def patch(self, request):
        class Availability(serializers.Serializer):
            available = serializers.BooleanField()
        serializer = Availability(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = get_object_or_404(Reviewer, user=request.user)
        profile.available = serializer.validated_data["available"]
        profile.save(update_fields=["available", "updated_at"])
        return Response(reviewer_data(profile))


class ResearchReviewerAdminView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def patch(self, request, reviewer_id):
        require_admin(request.user)
        profile = get_object_or_404(Reviewer, pk=reviewer_id)
        if profile.user_id == request.user.pk:
            raise PermissionDenied("Another administrator must approve your reviewer application.")
        class Decision(serializers.Serializer):
            approved = serializers.BooleanField()
        serializer = Decision(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile.approved = serializer.validated_data["approved"]
        profile.approved_by = request.user if profile.approved else None
        profile.save()
        if not profile.approved:
            ids = Assignment.objects.filter(reviewer=profile.user, active=True).values_list("submission_id", flat=True).distinct()
            for pk in ids:
                paper = Paper.objects.select_for_update().get(pk=pk)
                current_assignments(paper).filter(reviewer=profile.user).update(active=False, status="revoked")
                if paper.status in (*LIVE_STATES, Paper.Status.APPROVAL):
                    paper.status = Paper.Status.SUBMITTED
                    refresh_stage(paper)
                    event(paper, request.user, "reviewer_revoked")
                    fill_review_slots(paper)
        return Response(reviewer_data(profile, True))
