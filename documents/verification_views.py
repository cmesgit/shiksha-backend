"""API for community verification. Mounted under /api/explore/verification/.

Two blocks: the member-facing Verify page, and the admin's settings screen.

⚠ Every view here is gated on `GlobalSettings.explore_verification_enabled`
and returns **404** when it is off, not 403. A 403 tells a prober the endpoint
exists and is merely forbidden; while the feature is dark, the honest answer is
that there is nothing here.
"""
from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from global_settings.models import GlobalSettings

from . import verification as v
from .models import (
    Document,
    DocumentTopic,
    InviteState,
    SpecializationSource,
    SpecializationStatus,
    SubjectSpecialization,
    VerificationInvite,
    VerificationReview,
    VerificationSettings,
    VerificationStatus,
)
from .verification_serializers import (
    AdminReviewerSerializer,
    AttentionItemSerializer,
    MyUploadSerializer,
    QueueItemSerializer,
    ReviewTargetSerializer,
    SpecializationSerializer,
    SubmitReviewSerializer,
    VerificationSettingsSerializer,
)


def _require_feature():
    if not GlobalSettings.load().explore_verification_enabled:
        raise Http404
    return VerificationSettings.load()


def _is_verification_admin(user):
    """Superuser or an active ADMIN role — the same bar the research admin
    uses (`documents/research_workflow.py:26`).

    ⚠ NOT `is_staff`. Content editors carry `is_staff` so they can reach the
    Django admin for CMS work; it must not also hand them the switch that
    decides what the whole library counts as checked.
    """
    return bool(
        user and user.is_authenticated
        and (user.is_superuser or user.has_role("ADMIN"))
    )


class _VerificationView(APIView):
    """Shared feature gate + prefetches.

    `topics` and `files` are prefetched on every list: the serializers touch
    both per row, and without this a 20-row queue is 40 extra queries.
    """

    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        self.vsettings = _require_feature()
        super().initial(request, *args, **kwargs)

    def get_serializer_context(self, **extra):
        ctx = {"request": self.request, "vsettings": self.vsettings}
        ctx.update(extra)
        return ctx

    @staticmethod
    def _with_children(qs):
        return qs.select_related("owner", "category").prefetch_related(
            Prefetch("topics", queryset=DocumentTopic.objects.order_by("order")),
            "files",
        )


# ─────────────────────────────────────────────────────────────────────────────
# The Verify page
# ─────────────────────────────────────────────────────────────────────────────

class VerificationOverviewView(_VerificationView):
    """GET verification/ — everything the page's tab bar needs in one call.

    One request rather than five, because the tab labels carry counts and the
    page would otherwise render its own navigation wrong for a moment.
    """

    def get(self, request):
        queue = self._with_children(v.review_queue(request.user, self.vsettings))
        specs = SubjectSpecialization.objects.filter(user=request.user)
        mine = self._with_children(
            Document.objects.filter(owner=request.user, is_removed=False))
        unread = VerificationInvite.objects.filter(
            reviewer=request.user, state=InviteState.OPEN).count()

        subjects = v.confirmed_subjects(request.user)
        return Response({
            "enabled": True,
            # ⚠ Every number the page prints in prose comes from here, not
            # from a constant in the component. The Specializations intro and
            # the How-it-works rules both quote admin-editable thresholds, and
            # a hardcoded "2" keeps saying 2 after an admin sets it to 3 —
            # the page would then be explaining a rule the server no longer
            # enforces.
            "confirmations_needed": self.vsettings.confirmations_needed,
            "verified_uploads_to_earn_it": self.vsettings.verified_uploads_to_earn_it,
            "change_requests_before_hidden": self.vsettings.change_requests_before_hidden,
            "can_earn": self.vsettings.members_can_earn_it,
            "my_subjects": subjects,
            "queue_count": queue.count(),
            "notification_count": unread,
            "queue": QueueItemSerializer(
                queue[:50], many=True, context=self.get_serializer_context()).data,
            "specializations": SpecializationSerializer(
                specs, many=True, context=self.get_serializer_context()).data,
            "my_uploads": MyUploadSerializer(
                mine[:50], many=True, context=self.get_serializer_context()).data,
            # The chips under "Add a subject". Offer only what they could
            # actually claim — a subject they already hold in any state is not
            # a thing to request again.
            "available_subjects": _addable_subjects(request.user),
            "can_request": self.vsettings.members_can_request_it,
        })


def _addable_subjects(user):
    from .constants import SUBJECT_TOPICS
    held = {s.strip().casefold()
            for s in SubjectSpecialization.objects.filter(
                user=user).values_list("subject", flat=True)}
    return [s for s in SUBJECT_TOPICS if s.casefold() not in held]


class VerificationQueueView(_VerificationView):
    def get(self, request):
        queue = self._with_children(v.review_queue(request.user, self.vsettings))
        return Response({
            "count": queue.count(),
            "subjects": v.confirmed_subjects(request.user),
            "results": QueueItemSerializer(
                queue[:100], many=True,
                context=self.get_serializer_context()).data,
        })


class VerificationTargetView(_VerificationView):
    """GET  verification/sets/<id>/  — the review form's data.
    POST verification/sets/<id>/review/ — submit the three checks.
    """

    def get(self, request, document_id):
        doc = get_object_or_404(
            self._with_children(Document.objects.all()),
            pk=document_id, is_removed=False)
        return Response(ReviewTargetSerializer(
            doc, context=self.get_serializer_context()).data)

    def post(self, request, document_id):
        doc = get_object_or_404(Document, pk=document_id, is_removed=False)
        form = SubmitReviewSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        try:
            review, new_status = v.record_review(
                request.user, doc, settings=self.vsettings,
                **form.validated_data)
        except PermissionError as exc:
            # The service layer's reason is written for the member — "You
            # cannot review your own upload", not "Forbidden".
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({"note": [str(exc)]},
                            status=status.HTTP_400_BAD_REQUEST)

        doc.refresh_from_db()
        notify_review_outcome(doc, review, new_status)
        return Response({
            "outcome": review.outcome,
            "status": new_status,
            "progress": v.progress(doc, self.vsettings),
        }, status=status.HTTP_201_CREATED)


class NewVersionView(_VerificationView):
    """POST verification/sets/<id>/version/ — replace a set's files.

    ⚠ This endpoint is what makes two of the spec's six rules reachable at
    all: "uploading a new version clears earlier confirmations and restarts
    the count" and "editing the files of a Verified set returns it to
    Unverified". `bump_version()` existed and was tested from the start, but
    the only caller was the admin restore action — so the "Upload a new
    version" button on a member's own rejected set led to the generic upload
    form and produced a SECOND, SEPARATE document, leaving the rejected one
    frozen forever with nobody able to act on it.
    """

    def post(self, request, document_id):
        doc = get_object_or_404(Document, pk=document_id, is_removed=False)
        if doc.owner_id != request.user.id and not _is_verification_admin(request.user):
            return Response(
                {"detail": "Only the uploader can add a new version."},
                status=status.HTTP_403_FORBIDDEN)

        uploads = request.FILES.getlist("files") or request.FILES.getlist("file")
        if not uploads:
            return Response({"files": ["Attach at least one file."]},
                            status=status.HTTP_400_BAD_REQUEST)

        from django.core.exceptions import ValidationError as DjangoValidationError
        from config.upload_validation import (
            LIBRARY_EXTS, LIBRARY_MAX_MB, validate_upload,
        )
        for f in uploads:
            try:
                validate_upload(f, LIBRARY_EXTS, max_mb=LIBRARY_MAX_MB)
            except DjangoValidationError as exc:
                return Response(
                    {"files": [f"{getattr(f, 'name', 'A file')}: "
                               f"{'; '.join(exc.messages)}"]},
                    status=status.HTTP_400_BAD_REQUEST)

        from .models import DocumentFile, compute_file_hash

        with transaction.atomic():
            # The previous version's files go, the previous version's REVIEWS
            # stay. The reviews are a record of what a person actually said and
            # the history panel is supposed to show that someone asked for
            # changes and the uploader fixed them; they stop counting because
            # the count is per version, not because they were deleted.
            doc.files.all().delete()
            for order, f in enumerate(uploads):
                DocumentFile.objects.create(
                    document=doc, owner_id=doc.owner_id, file=f,
                    original_name=getattr(f, "name", "") or "",
                    filetype=(getattr(f, "name", "").rsplit(".", 1)[-1]
                              or "").upper()[:10],
                    size=getattr(f, "size", 0) or 0,
                    file_hash=compute_file_hash(f), order=order,
                )
            first = doc.files.first()
            if first is not None:
                # Keep the parent's legacy columns pointing at the set's first
                # file — the reader and the download endpoint still use them.
                doc.file = first.file
                doc.file_hash = first.file_hash
                doc.filetype = first.filetype
                doc.save(update_fields=["file", "file_hash", "filetype"])

            v.bump_version(doc, self.vsettings)

        doc.refresh_from_db()
        # A fresh version needs fresh reviewers; the old invites were
        # superseded by bump_version.
        from .verification_signals import invite_for
        invite_for(doc)

        return Response({
            "version": doc.verification_version,
            "status": doc.verification_status,
            "progress": v.progress(doc, self.vsettings),
        }, status=status.HTTP_201_CREATED)


class VerificationNotificationsView(_VerificationView):
    """The Notifications tab.

    Built from invites and reviews rather than from a feed table. These are
    derived facts — "you were invited", "your set was verified" — and a second
    store of them would go stale the moment a set moved on. The platform's own
    notification pipeline still fires separately for email/in-app.
    """

    def get(self, request):
        items = []

        for invite in VerificationInvite.objects.filter(
            reviewer=request.user, state=InviteState.OPEN,
        ).select_related("document", "document__owner")[:50]:
            doc = invite.document
            overdue_soon = (
                invite.expires_at
                and (invite.expires_at - timezone.now()).days <= 1
            )
            items.append({
                "kind": "reminder" if overdue_soon else "invite",
                "title": (
                    f'Reminder: 1 day left to review "{doc.title}"'
                    if overdue_soon
                    else f'You are invited to review "{doc.title}"'
                ),
                "sub": f"{invite.subject} · {doc.owner.get_full_name() or doc.owner.username}",
                "action": "review",
                "document_id": doc.id,
                "unread": invite.read_at is None,
            })

        for doc in Document.objects.filter(
            owner=request.user, is_removed=False,
            verification_status=VerificationStatus.VERIFIED,
        ).order_by("-verified_at")[:20]:
            items.append({
                "kind": "verified",
                "title": f'"{doc.title}" is now Verified',
                "sub": f"Confirmed by {v.confirmation_count(doc)} specialists",
                "action": "view",
                "document_id": doc.id,
                "unread": False,
            })

        for review in VerificationReview.objects.filter(
            document__owner=request.user,
            outcome="changes_requested",
        ).select_related("document").order_by("-created_at")[:20]:
            if review.version != review.document.verification_version:
                continue  # already addressed by a new version
            items.append({
                "kind": "changes",
                "title": f'Changes requested on "{review.document.title}"',
                "sub": review.note,
                "action": "note",
                "document_id": review.document_id,
                "unread": True,
            })

        return Response({
            "unread": sum(1 for i in items if i["unread"]),
            "results": items,
        })


class VerificationNotificationsReadView(_VerificationView):
    """Mark all as read.

    ⚠ This used to stamp `reminder_sent_at`, borrowing it as a read-marker.
    That field is also what the queue row renders as "· reminder sent", so
    clicking "Mark all as read" made the queue claim a reminder had been sent
    that never was — and suppressed the real reminder the sweep would have
    sent. Read state is its own column.
    """

    def post(self, request):
        VerificationInvite.objects.filter(
            reviewer=request.user, state=InviteState.OPEN, read_at__isnull=True,
        ).update(read_at=timezone.now())
        return Response({"ok": True})


class SpecializationsView(_VerificationView):
    """POST a request to review a subject. Never grants it.

    "A member sends proof of their background and an admin approves or
    declines." The row is created PENDING; only the admin endpoint confirms.
    """

    def post(self, request):
        if not self.vsettings.members_can_request_it:
            return Response(
                {"detail": "Requests to review are closed at the moment."},
                status=status.HTTP_403_FORBIDDEN)

        subject = (request.data.get("subject") or "").strip()
        if not subject:
            return Response({"subject": ["Choose a subject."]},
                            status=status.HTTP_400_BAD_REQUEST)

        existing = SubjectSpecialization.objects.filter(
            user=request.user, subject__iexact=subject).first()
        if existing is not None:
            return Response(
                {"detail": f"You already have a {existing.get_status_display().lower()} "
                           f"request for {existing.subject}."},
                status=status.HTTP_409_CONFLICT)

        spec = SubjectSpecialization.objects.create(
            user=request.user, subject=subject,
            source=SpecializationSource.APPROVED,
            status=SpecializationStatus.PENDING,
            evidence=(request.data.get("evidence") or "").strip(),
        )
        return Response(
            SpecializationSerializer(
                spec, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED)


class SpecializationDetailView(_VerificationView):
    def delete(self, request, spec_id):
        spec = get_object_or_404(
            SubjectSpecialization, pk=spec_id, user=request.user)
        spec.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────────────────────────────────────────────────────
# Admin
# ─────────────────────────────────────────────────────────────────────────────

class _VerificationAdminView(_VerificationView):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not _is_verification_admin(request.user):
            raise Http404


class AdminSettingsView(_VerificationAdminView):
    def get(self, request):
        return Response(VerificationSettingsSerializer(self.vsettings).data)

    def patch(self, request):
        form = VerificationSettingsSerializer(
            self.vsettings, data=request.data, partial=True)
        form.is_valid(raise_exception=True)
        form.save()
        return Response(VerificationSettingsSerializer(
            VerificationSettings.load()).data)


class AdminReviewersView(_VerificationAdminView):
    def get(self, request):
        ctx = self.get_serializer_context()
        requests_qs = SubjectSpecialization.objects.filter(
            status=SpecializationStatus.PENDING).select_related("user")
        reviewers_qs = SubjectSpecialization.objects.filter(
            status=SpecializationStatus.CONFIRMED).select_related("user")
        return Response({
            "requests": AdminReviewerSerializer(
                requests_qs, many=True, context=ctx).data,
            "reviewers": AdminReviewerSerializer(
                reviewers_qs, many=True, context=ctx).data,
        })


class AdminReviewerActionView(_VerificationAdminView):
    """approve / decline / pause / resume on one specialization."""

    ACTIONS = {"approve", "decline", "pause", "resume"}

    def post(self, request, spec_id, action):
        if action not in self.ACTIONS:
            raise Http404
        spec = get_object_or_404(SubjectSpecialization, pk=spec_id)

        if action == "approve":
            spec.status = SpecializationStatus.CONFIRMED
            spec.source = SpecializationSource.APPROVED
        elif action == "decline":
            spec.status = SpecializationStatus.DECLINED
        elif action == "pause":
            spec.is_paused = True
        else:
            spec.is_paused = False
            # Resuming forgives the run of missed invites. Without this the
            # reviewer is paused again by the very next expiry, and the
            # admin's action looks like it did nothing.
            spec.missed_invites = 0

        spec.decided_by = request.user
        spec.decided_at = timezone.now()
        spec.save()
        return Response(AdminReviewerSerializer(
            spec, context=self.get_serializer_context()).data)


class AdminAttentionView(_VerificationAdminView):
    """"Most sets never appear here. This list only holds the ones the
    automatic flow could not finish." The count of the ones that ARE flowing
    is returned alongside, because that sentence is what makes a short list
    reassuring rather than suspicious."""

    def get(self, request):
        candidates = self._with_children(
            Document.objects.filter(is_removed=False).exclude(
                verification_status=VerificationStatus.VERIFIED))

        reasons, flagged, automatic = {}, [], 0
        for doc in candidates[:500]:
            reason = v.needs_admin_attention(doc, self.vsettings)
            if reason:
                reasons[doc.id] = reason
                flagged.append(doc)
            else:
                automatic += 1

        return Response({
            "handled_automatically": automatic,
            "results": AttentionItemSerializer(
                flagged, many=True,
                context=self.get_serializer_context(reasons=reasons)).data,
        })


class AdminAttentionActionView(_VerificationAdminView):
    """assign / restore / remove on one flagged set."""

    def post(self, request, document_id, action):
        doc = get_object_or_404(Document, pk=document_id)

        if action == "assign":
            reviewer_id = request.data.get("reviewer_id")
            if not reviewer_id:
                return Response(
                    {"reviewer_id": ["Choose a reviewer."]},
                    status=status.HTTP_400_BAD_REQUEST)
            spec = get_object_or_404(
                SubjectSpecialization, pk=reviewer_id,
                status=SpecializationStatus.CONFIRMED)
            if spec.user_id == doc.owner_id:
                return Response(
                    {"detail": "Nobody can review their own upload."},
                    status=status.HTTP_400_BAD_REQUEST)
            invite, _ = VerificationInvite.objects.get_or_create(
                document=doc, version=doc.verification_version,
                reviewer_id=spec.user_id,
                defaults={
                    "subject": spec.subject,
                    "assigned_by_admin": True,
                    "expires_at": None,
                },
            )
            # Re-opening a previously expired invite is the point of the
            # action: the admin is overriding the sweep that closed it.
            if invite.state != InviteState.OPEN:
                invite.state = InviteState.OPEN
                invite.assigned_by_admin = True
                invite.save(update_fields=["state", "assigned_by_admin"])
            return Response({"ok": True, "invite_id": invite.id})

        if action == "restore":
            doc.verification_status = VerificationStatus.UNVERIFIED
            doc.save(update_fields=["verification_status"])
            # A restore that left the change requests standing would be undone
            # by the next recount — the set would go straight back to hidden.
            # Bumping the version is the honest way to say "look at it again".
            v.bump_version(doc)
            return Response({"ok": True, "status": VerificationStatus.UNVERIFIED})

        if action == "remove":
            doc.is_removed = True
            doc.removed_at = timezone.now()
            doc.save(update_fields=["is_removed", "removed_at"])
            return Response({"ok": True, "removed": True})

        raise Http404


class AdminEligibleReviewersView(_VerificationAdminView):
    """Who the admin may hand a set to, for the assign dialog.

    Deliberately the same `eligible_reviewers()` the automatic flow uses, so
    the picker cannot offer someone the server would then refuse.
    """

    def get(self, request, document_id):
        doc = get_object_or_404(Document, pk=document_id)
        specs = v.eligible_reviewers(doc, self.vsettings)
        if not specs:
            # The flow already failed to find anyone — fall back to every
            # confirmed specialist in the set's subjects so the admin has
            # something to act on, which is the whole point of this screen.
            specs = list(SubjectSpecialization.objects.filter(
                status=SpecializationStatus.CONFIRMED,
                subject__in=v.subjects_of(doc),
            ).exclude(user_id=doc.owner_id).select_related("user"))
        return Response(AdminReviewerSerializer(
            specs, many=True, context=self.get_serializer_context()).data)


# ─────────────────────────────────────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────────────────────────────────────

def notify_invite(invite, reminder=False):
    """"You are invited to review X" / "Reminder: N days left".

    ⚠ This verb existed in notifications/policy.py from the start and had no
    sender, because nothing created invites. In-app + email opt-out, SMS OFF —
    a specialist can be invited to several sets a day, and this platform's SMS
    is policy-OFF anyway.
    """
    try:
        from notifications.services import notify
    except Exception:
        return
    try:
        doc = invite.document
        if reminder:
            left = ""
            if invite.expires_at:
                days = max(0, (invite.expires_at - timezone.now()).days)
                left = f"{days} day{'' if days == 1 else 's'} left to review"
            title = f'Reminder: {left or "your review is due"} "{doc.title}"'
        else:
            title = f'You are invited to review "{doc.title}"'
        notify(
            recipient=invite.reviewer,
            verb="explore.review_invited",
            title=title,
            body=f"{invite.subject} · uploaded by "
                 f"{doc.owner.get_full_name() or doc.owner.username}",
            link_url=f"/explore/verify?tab=queue&set={doc.pk}",
        )
    except Exception:
        return


def notify_review_outcome(document, review, new_status):
    """Hand off to the platform's own policy-driven pipeline.

    In-app and email (opt-out) only. **No SMS and no push** — the same choice
    `research.updated` makes: SMS is policy-OFF platform-wide and
    `fcm-django` is not installed, so a push verb here would be a verb that
    silently never delivers.

    Wrapped in a broad except on purpose: a notification backend being down
    must not roll back a review that was already written and counted.
    """
    try:
        from notifications.services import notify
    except Exception:
        return

    try:
        if new_status == VerificationStatus.VERIFIED:
            notify(
                recipient=document.owner, verb="explore.verified",
                title=f'"{document.title}" is now Verified',
                body=f"Confirmed by {v.confirmation_count(document)} specialists.",
                link_url=f"/explore/doc/{document.pk}",
            )
        elif review.outcome == "changes_requested":
            notify(
                recipient=document.owner, verb="explore.changes_requested",
                title=f'Changes requested on "{document.title}"',
                body=review.note,
                link_url="/explore/verify?tab=uploads",
            )
    except Exception:
        return
