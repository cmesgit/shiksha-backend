# documents/moderation_views.py — Explore Moderation panel endpoints.
#
# All gated by IsDocumentsModerator (staff / documents.moderate / MODERATOR).
# Mounted under /api/explore/mod/... Mirrors forum/moderation_views.py; the
# Explore-specific section is Duplicate Review (DuplicateFlag queue). The four
# panel sections are: Reported Documents, Duplicate Review, Uploader
# Management, Analytics.

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Document, DocumentProfile, Report, ModerationAction, DuplicateFlag,
)
from .permissions import IsDocumentsModerator
from .utils import contributor_badge
from .views import _int_param
from notifications.services import notify

User = get_user_model()

# Reasons that count as "high priority" for the header stat.
HIGH_SEVERITY_REASONS = {"copyright", "plagiarism"}


# =====================================================
# Shared helpers
# =====================================================
def _document_snapshot(doc):
    return {
        "title": doc.title,
        "snippet": (doc.description or doc.full or "")[:220],
        "filetype": doc.filetype,
        "pages": doc.pages,
        "category": doc.category.name if doc.category_id else "Document",
    }


def _log_action(moderator, action, target_user=None, target=None, note=""):
    ct = ContentType.objects.get_for_model(target) if target is not None else None
    oid = target.pk if target is not None else None
    ModerationAction.objects.create(
        moderator=moderator, action=action, target_user=target_user,
        content_type=ct, object_id=oid, note=note,
    )


def _notify_user(user, actor, verb, message, note=""):
    notify(
        recipient=user, actor=actor, verb=verb, title=message, body=message,
        link_url="/explore", payload={"note": note},
    )


def _ban_user(user, note):
    profile, _ = DocumentProfile.objects.get_or_create(user=user)
    profile.is_banned = True
    profile.ban_reason = note
    profile.save(update_fields=["is_banned", "ban_reason"])


def _suspend_user(user, duration_days, note):
    profile, _ = DocumentProfile.objects.get_or_create(user=user)
    until = timezone.now() + timedelta(days=max(1, int(duration_days or 7)))
    profile.suspended_until = until
    profile.save(update_fields=["suspended_until"])
    return until


def _reinstate_user(user):
    profile, _ = DocumentProfile.objects.get_or_create(user=user)
    profile.is_banned = False
    profile.ban_reason = ""
    profile.suspended_until = None
    profile.save(update_fields=["is_banned", "ban_reason", "suspended_until"])


def _remove_document(doc):
    doc.is_removed = True
    doc.removed_at = timezone.now()
    doc.save(update_fields=["is_removed", "removed_at"])


# =====================================================
# Reported Documents
# =====================================================
class ModReportsView(APIView):
    permission_classes = [IsDocumentsModerator]

    def get(self, request):
        reason = request.query_params.get("reason")
        status_param = request.query_params.get("status", "pending")
        qs = Report.objects.select_related("reporter", "content_type").order_by("-created_at")
        if status_param == "resolved":
            qs = qs.filter(resolved=True)
        elif status_param != "all":
            qs = qs.filter(resolved=False)
        if reason and reason != "all":
            qs = qs.filter(reason=reason)

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 20, 100)
        total = qs.count()
        start = (page - 1) * page_size
        rows = list(qs[start:start + page_size])

        # Resolve document targets in one batched query.
        doc_ids = [r.object_id for r in rows]
        docs = {
            d.pk: d for d in Document.objects.select_related("owner", "category")
            .filter(pk__in=doc_ids)
        }

        results = []
        for r in rows:
            doc = docs.get(r.object_id)
            if doc is None:
                continue
            report_count = Report.objects.filter(
                content_type_id=r.content_type_id, object_id=r.object_id).count()
            snap = _document_snapshot(doc)
            results.append({
                "id": r.id, "reason": r.reason,
                "reason_label": dict(Report.REASON_CHOICES).get(r.reason, r.reason),
                "detail": r.detail,
                "content_type": snap["category"],
                "content_title": snap["title"], "snippet": snap["snippet"],
                "filetype": snap["filetype"], "pages": snap["pages"],
                "reporter": contributor_badge(r.reporter),
                "uploader": contributor_badge(doc.owner),
                "report_count": report_count,
                "resolved": r.resolved, "created_at": r.created_at,
            })
        return Response({"results": results, "count": total})


class ModReportDismissView(APIView):
    permission_classes = [IsDocumentsModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        report.resolved = True
        report.resolved_at = timezone.now()
        report.save(update_fields=["resolved", "resolved_at"])
        _log_action(request.user, ModerationAction.ACTION_DISMISS,
                    note=request.data.get("note", ""))
        return Response({"resolved": True})


class ModReportRemoveView(APIView):
    permission_classes = [IsDocumentsModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        doc = report.target
        author = doc.owner if isinstance(doc, Document) else None
        _log_action(request.user, ModerationAction.ACTION_REMOVE, target_user=author,
                    target=doc, note=request.data.get("note", ""))
        if isinstance(doc, Document):
            _remove_document(doc)
        Report.objects.filter(
            content_type_id=report.content_type_id, object_id=report.object_id
        ).update(resolved=True, resolved_at=timezone.now())
        return Response({"removed": True})


class ModReportWarnView(APIView):
    permission_classes = [IsDocumentsModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        doc = report.target
        author = doc.owner if isinstance(doc, Document) else None
        if author is None:
            return Response({"detail": "Could not resolve the document's uploader."},
                            status=status.HTTP_400_BAD_REQUEST)
        note = request.data.get("note", "")
        report.resolved = True
        report.resolved_at = timezone.now()
        report.save(update_fields=["resolved", "resolved_at"])
        _log_action(request.user, ModerationAction.ACTION_WARN, target_user=author, note=note)
        message = "You've received a formal warning about one of your Explore uploads." + (f" Note: {note}" if note else "")
        _notify_user(author, request.user, "explore.warned", message, note)
        return Response({"warned": True})


class ModReportSuspendView(APIView):
    permission_classes = [IsDocumentsModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        doc = report.target
        author = doc.owner if isinstance(doc, Document) else None
        if author is None:
            return Response({"detail": "Could not resolve the document's uploader."},
                            status=status.HTTP_400_BAD_REQUEST)
        note = request.data.get("note", "")
        until = _suspend_user(author, request.data.get("duration_days", 7), note)
        report.resolved = True
        report.resolved_at = timezone.now()
        report.save(update_fields=["resolved", "resolved_at"])
        _log_action(request.user, ModerationAction.ACTION_SUSPEND, target_user=author, note=note)
        message = f"Your Explore upload access is suspended until {until:%d %b %Y}." + (f" Reason: {note}" if note else "")
        _notify_user(author, request.user, "explore.suspended", message, note)
        return Response({"suspended": True, "suspended_until": until})


class ModReportBanView(APIView):
    permission_classes = [IsDocumentsModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        doc = report.target
        author = doc.owner if isinstance(doc, Document) else None
        if author is None:
            return Response({"detail": "Could not resolve the document's uploader."},
                            status=status.HTTP_400_BAD_REQUEST)
        note = request.data.get("note", "")
        _ban_user(author, note)
        report.resolved = True
        report.resolved_at = timezone.now()
        report.save(update_fields=["resolved", "resolved_at"])
        _log_action(request.user, ModerationAction.ACTION_BAN, target_user=author, note=note)
        message = "You have been banned from uploading to the Explore library." + (f" Reason: {note}" if note else "")
        _notify_user(author, request.user, "explore.banned", message, note)
        return Response({"banned": True})


# =====================================================
# Duplicate Review
# =====================================================
class ModDuplicatesView(APIView):
    permission_classes = [IsDocumentsModerator]

    def get(self, request):
        status_param = request.query_params.get("status", "pending")
        qs = DuplicateFlag.objects.select_related(
            "document", "document__owner", "original", "original__owner"
        ).order_by("-created_at")
        if status_param != "all":
            qs = qs.filter(status=status_param)

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 20, 100)
        total = qs.count()
        start = (page - 1) * page_size
        rows = qs[start:start + page_size]

        results = [{
            "id": f.id,
            "similarity": f.similarity,
            "note": f.note,
            "status": f.status,
            "created_at": f.created_at,
            "document": {
                "id": f.document_id, "title": f.document.title,
                "filetype": f.document.filetype,
                "uploader": contributor_badge(f.document.owner),
            },
            "original": ({
                "id": f.original_id, "title": f.original.title,
                "uploader": contributor_badge(f.original.owner),
            } if f.original_id else None),
        } for f in rows]
        return Response({"results": results, "count": total})


class ModDuplicateConfirmView(APIView):
    """Confirm the flag → soft-remove the duplicate document."""
    permission_classes = [IsDocumentsModerator]

    def post(self, request, flag_id):
        flag = get_object_or_404(DuplicateFlag, pk=flag_id)
        _remove_document(flag.document)
        flag.status = DuplicateFlag.STATUS_CONFIRMED
        flag.reviewed_by = request.user
        flag.reviewed_at = timezone.now()
        flag.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        _log_action(request.user, ModerationAction.ACTION_REMOVE,
                    target_user=flag.document.owner, target=flag.document,
                    note=request.data.get("note", "Confirmed duplicate"))
        return Response({"status": flag.status})


class ModDuplicateDismissView(APIView):
    """Dismiss the flag → keep the document live."""
    permission_classes = [IsDocumentsModerator]

    def post(self, request, flag_id):
        flag = get_object_or_404(DuplicateFlag, pk=flag_id)
        flag.status = DuplicateFlag.STATUS_DISMISSED
        flag.reviewed_by = request.user
        flag.reviewed_at = timezone.now()
        flag.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        _log_action(request.user, ModerationAction.ACTION_DISMISS,
                    target=flag.document, note=request.data.get("note", ""))
        return Response({"status": flag.status})


# =====================================================
# Uploader Management
# =====================================================
def _uploader_stats(user, doc_ct):
    doc_ids = list(Document.objects.filter(owner=user).values_list("id", flat=True))
    uploads_count = len(doc_ids)
    reports_count = Report.objects.filter(content_type=doc_ct, object_id__in=doc_ids).count()
    profile = getattr(user, "document_profile", None)
    banned = bool(profile and profile.is_banned)
    suspended_until = profile.suspended_until if profile else None
    suspended = bool(suspended_until and suspended_until > timezone.now())
    if not suspended:
        suspended_until = None
    warned = (not banned and not suspended) and ModerationAction.objects.filter(
        action=ModerationAction.ACTION_WARN, target_user=user).exists()
    status_label = (
        "banned" if banned else
        "suspended" if suspended else
        "warned" if warned else
        "active"
    )
    return uploads_count, reports_count, status_label, suspended_until


class ModUploadersView(APIView):
    permission_classes = [IsDocumentsModerator]

    def get(self, request):
        search = (request.query_params.get("search") or "").strip()
        status_filter = request.query_params.get("status", "all")
        doc_ct = ContentType.objects.get_for_model(Document)

        candidate_ids = (
            set(Document.objects.values_list("owner_id", flat=True))
            | set(DocumentProfile.objects.values_list("user_id", flat=True))
        )
        qs = User.objects.filter(id__in=candidate_ids).select_related("document_profile")
        if search:
            qs = qs.filter(Q(username__icontains=search) | Q(email__icontains=search))
        qs = qs.order_by("username")

        enriched = []
        for u in qs:
            uploads, reports, status_label, suspended_until = _uploader_stats(u, doc_ct)
            if status_filter != "all" and status_label != status_filter:
                continue
            badge = contributor_badge(u)
            enriched.append({
                "id": str(u.id), "username": u.username, "email": u.email,
                "name": badge["name"], "initials": badge["initials"], "color": badge["color"],
                "uploads": uploads, "reports": reports, "status": status_label,
                "suspended_until": suspended_until,
            })

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 20, 100)
        total = len(enriched)
        start = (page - 1) * page_size
        return Response({"results": enriched[start:start + page_size], "count": total})


class ModUploaderWarnView(APIView):
    permission_classes = [IsDocumentsModerator]

    def post(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        note = request.data.get("note", "")
        _log_action(request.user, ModerationAction.ACTION_WARN, target_user=user, note=note)
        message = "You've received a formal warning from an Explore moderator." + (f" Note: {note}" if note else "")
        _notify_user(user, request.user, "explore.warned", message, note)
        return Response({"warned": True})


class ModUploaderSuspendView(APIView):
    permission_classes = [IsDocumentsModerator]

    def post(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        note = request.data.get("note", "")
        until = _suspend_user(user, request.data.get("duration_days", 7), note)
        _log_action(request.user, ModerationAction.ACTION_SUSPEND, target_user=user, note=note)
        message = f"Your Explore upload access is suspended until {until:%d %b %Y}." + (f" Reason: {note}" if note else "")
        _notify_user(user, request.user, "explore.suspended", message, note)
        return Response({"suspended": True, "suspended_until": until})


class ModUploaderBanView(APIView):
    permission_classes = [IsDocumentsModerator]

    def post(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        note = request.data.get("note", "")
        _ban_user(user, note)
        _log_action(request.user, ModerationAction.ACTION_BAN, target_user=user, note=note)
        message = "You have been banned from uploading to the Explore library." + (f" Reason: {note}" if note else "")
        _notify_user(user, request.user, "explore.banned", message, note)
        return Response({"banned": True})


class ModUploaderUnbanView(APIView):
    """Reinstate an uploader whether they were banned or suspended."""
    permission_classes = [IsDocumentsModerator]

    def post(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        _reinstate_user(user)
        _log_action(request.user, ModerationAction.ACTION_UNBAN, target_user=user,
                    note=request.data.get("note", ""))
        message = "Your Explore upload access has been fully restored."
        _notify_user(user, request.user, "explore.unbanned", message)
        return Response({"banned": False})


# =====================================================
# Activity Log
# =====================================================
_LOG_META = {
    ModerationAction.ACTION_DISMISS: ("ok", "Dismissed"),
    ModerationAction.ACTION_REMOVE: ("bad", "Removed"),
    ModerationAction.ACTION_WARN: ("warn", "Warned"),
    ModerationAction.ACTION_BAN: ("bad", "Banned"),
    ModerationAction.ACTION_UNBAN: ("ok", "Reinstated"),
    ModerationAction.ACTION_RESTORE: ("ok", "Restored"),
    ModerationAction.ACTION_SUSPEND: ("warn", "Suspended"),
    ModerationAction.ACTION_LOCK: ("warn", "Locked"),
    ModerationAction.ACTION_UNLOCK: ("ok", "Unlocked"),
}


class ModLogView(APIView):
    permission_classes = [IsDocumentsModerator]

    def get(self, request):
        qs = ModerationAction.objects.select_related("moderator", "target_user").order_by("-created_at")
        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 20, 100)
        total = qs.count()
        start = (page - 1) * page_size
        rows = list(qs[start:start + page_size])

        by_ct = {}
        for a in rows:
            if a.content_type_id and a.object_id:
                by_ct.setdefault(a.content_type_id, []).append(a.object_id)
        targets = {}
        for ct_id, ids in by_ct.items():
            model = ContentType.objects.get(pk=ct_id).model_class()
            for obj in model.objects.filter(pk__in=ids):
                targets[(ct_id, obj.pk)] = obj

        results = []
        for a in rows:
            target = targets.get((a.content_type_id, a.object_id))
            action_type, label = _LOG_META.get(a.action, ("ok", a.get_action_display()))
            title = getattr(target, "title", None) if target is not None else None
            if title:
                text = f"{label} document “{title}”"
            elif a.target_user:
                text = f"{label} {a.target_user.username}"
            else:
                text = label
            results.append({
                "id": a.id, "action": a.action, "type": action_type, "label": label,
                "text": text, "note": a.note,
                "moderator": a.moderator.username if a.moderator else "—",
                "target_user": a.target_user.username if a.target_user else None,
                "created_at": a.created_at,
            })
        return Response({"results": results, "count": total})


# =====================================================
# Analytics
# =====================================================
class ModAnalyticsView(APIView):
    permission_classes = [IsDocumentsModerator]

    def get(self, request):
        now = timezone.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_month_end = month_start - timedelta(seconds=1)
        prev_month_start = prev_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_ago = now - timedelta(days=30)

        def pct_change(cur, prev):
            if not prev:
                return None
            return round((cur - prev) / prev * 100, 1)

        reports_this_month = Report.objects.filter(created_at__gte=month_start).count()
        reports_prev_month = Report.objects.filter(
            created_at__gte=prev_month_start, created_at__lt=month_start).count()
        uploads_this_month = Document.objects.filter(created_at__gte=month_start).count()
        banned_this_month = ModerationAction.objects.filter(
            action=ModerationAction.ACTION_BAN, created_at__gte=month_start).count()
        banned_prev_month = ModerationAction.objects.filter(
            action=ModerationAction.ACTION_BAN,
            created_at__gte=prev_month_start, created_at__lt=month_start).count()

        kpis = [
            {"label": "Reports this month", "value": reports_this_month,
             "trend": pct_change(reports_this_month, reports_prev_month), "direction": "bad_if_up"},
            {"label": "Uploads this month", "value": uploads_this_month,
             "trend": None, "direction": "good_if_up"},
            {"label": "Duplicate flags (30d)", "value": DuplicateFlag.objects.filter(created_at__gte=month_ago).count(),
             "trend": None, "direction": "bad_if_up"},
            {"label": "Uploaders banned", "value": banned_this_month,
             "trend": pct_change(banned_this_month, banned_prev_month), "direction": "good_if_down"},
        ]

        reason_counts = {key: 0 for key, _ in Report.REASON_CHOICES}
        for row in Report.objects.filter(created_at__gte=month_ago).values("reason").annotate(n=Count("id")):
            reason_counts[row["reason"]] = row["n"]
        max_count = max(reason_counts.values()) or 1
        reports_by_reason = [
            {"reason": key, "label": label, "count": reason_counts[key],
             "pct": round(reason_counts[key] / max_count * 100)}
            for key, label in Report.REASON_CHOICES
        ]

        recent_actions = [{
            "id": a.id, "action": a.action,
            "moderator": a.moderator.username if a.moderator else "—",
            "target_user": a.target_user.username if a.target_user else None,
            "note": a.note, "created_at": a.created_at,
        } for a in ModerationAction.objects.select_related("moderator", "target_user").order_by("-created_at")[:10]]

        this_month = {
            "reports_resolved": Report.objects.filter(resolved=True, resolved_at__gte=month_start).count(),
            "uploads_published": uploads_this_month,
            "uploaders_warned": ModerationAction.objects.filter(
                action=ModerationAction.ACTION_WARN, created_at__gte=month_start).count(),
            "uploaders_suspended": ModerationAction.objects.filter(
                action=ModerationAction.ACTION_SUSPEND, created_at__gte=month_start).count(),
            "uploaders_banned": banned_this_month,
        }

        pending_reports = Report.objects.filter(resolved=False)
        header_stats = {
            "reported_docs": pending_reports.count(),
            "high_priority": pending_reports.filter(reason__in=HIGH_SEVERITY_REASONS).count(),
            "duplicate_uploads": DuplicateFlag.objects.filter(
                status=DuplicateFlag.STATUS_PENDING).count(),
            "banned_uploaders": DocumentProfile.objects.filter(is_banned=True).count(),
        }

        return Response({
            "kpis": kpis,
            "reports_by_reason": reports_by_reason,
            "recent_actions": recent_actions,
            "this_month": this_month,
            "header_stats": header_stats,
        })
