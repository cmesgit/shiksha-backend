# forum/moderation_views.py — Moderator Panel endpoints (all gated by
# IsForumModerator: staff or a user with the MODERATOR role). Mounted under
# /api/forum/mod/... in forum/urls.py.

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin
from . import moderation as forum_moderation
from .models import (
    ForumPost, Reply, ForumProfile, Report, ModerationAction,
    AutoRejectedSubmission, Tag, ForumCategory,
)
from .permissions import IsForumModerator
from .serializers import ForumCategorySerializer, CategoryWriteSerializer
from .utils import author_badge
from .views import _int_param, _annotated_threads
from notifications.services import notify

User = get_user_model()

# Which Report.REASON_CHOICES count as high-severity, for the "High
# priority" header stat.
HIGH_SEVERITY_REASONS = {"abusive"}


# =====================================================
# Shared helpers
# =====================================================

def _content_label(obj):
    if isinstance(obj, ForumPost):
        return "Post" if obj.kind == ForumPost.KIND_POST else "Question"
    if isinstance(obj, Reply):
        return "Comment" if obj.kind == Reply.KIND_COMMENT else "Answer"
    return "Content"


def _content_snapshot(obj):
    if isinstance(obj, ForumPost):
        return {"title": obj.title, "snippet": (obj.content or "")[:220]}
    if isinstance(obj, Reply):
        return {"title": obj.post.title if obj.post_id else "", "snippet": (obj.content or "")[:220]}
    return {"title": "", "snippet": ""}


def _target_author(obj):
    return getattr(obj, "author", None)


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
        link_url="/forum", payload={"note": note},
    )


def _ban_user(user, note):
    profile, _ = ForumProfile.objects.get_or_create(user=user)
    profile.is_banned = True
    profile.ban_reason = note
    profile.save(update_fields=["is_banned", "ban_reason"])


def _suspend_user(user, duration_days, note):
    profile, _ = ForumProfile.objects.get_or_create(user=user)
    until = timezone.now() + timedelta(days=max(1, int(duration_days or 7)))
    profile.suspended_until = until
    profile.save(update_fields=["suspended_until"])
    return until


def _reinstate_user(user):
    """Clears both a ban and a suspension — used for the single "Reinstate"
    action, which the moderator UI shows for either state."""
    profile, _ = ForumProfile.objects.get_or_create(user=user)
    profile.is_banned = False
    profile.ban_reason = ""
    profile.suspended_until = None
    profile.save(update_fields=["is_banned", "ban_reason", "suspended_until"])


def _target_thread(obj):
    """Resolve the ForumPost a report target belongs to, whether the report
    is against the thread itself or a reply within it."""
    if isinstance(obj, ForumPost):
        return obj
    if isinstance(obj, Reply):
        return obj.post
    return None


def _remove_content(obj):
    """Soft-delete forum content so it can be restored and so counts/threads
    stay consistent. Both ForumPost and Reply carry ``is_removed``."""
    if isinstance(obj, ForumPost):
        obj.is_removed = True
        obj.removed_at = timezone.now()
        obj.save(update_fields=["is_removed", "removed_at"])
    elif isinstance(obj, Reply):
        obj.is_removed = True
        obj.removed_at = timezone.now()
        obj.save(update_fields=["is_removed", "removed_at"])
        # If this reply was the accepted answer, clear the pointer so the
        # thread doesn't display a hidden reply as "solved".
        post = obj.post
        if post.accepted_reply_id == obj.id:
            post.accepted_reply = None
            post.is_solved = False
            post.save(update_fields=["accepted_reply", "is_solved"])
    else:
        obj.delete()


# =====================================================
# Reported Content
# =====================================================
class ModReportsView(APIView):
    permission_classes = [IsForumModerator]

    def get(self, request):
        reason = request.query_params.get("reason")
        status_param = request.query_params.get("status", "pending")
        qs = Report.objects.select_related("reporter", "content_type").order_by("-created_at")
        if status_param == "resolved":
            qs = qs.filter(resolved=True)
        elif status_param != "all":
            qs = qs.filter(resolved=False)
        if reason:
            qs = qs.filter(reason=reason)

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 20, 100)
        total = qs.count()
        start = (page - 1) * page_size
        rows = list(qs[start:start + page_size])

        # GenericForeignKey can't select_related, so resolve targets in
        # batches grouped by content type instead of one query per row.
        by_ct = {}
        for r in rows:
            by_ct.setdefault(r.content_type_id, []).append(r.object_id)
        targets = {}
        for ct_id, ids in by_ct.items():
            model = ContentType.objects.get(pk=ct_id).model_class()
            related = ("author", "post") if model is Reply else ("author",)
            for obj in model.objects.select_related(*related).filter(pk__in=ids):
                targets[(ct_id, obj.pk)] = obj

        results = []
        for r in rows:
            target = targets.get((r.content_type_id, r.object_id))
            if target is None:
                continue  # underlying content already gone
            author = _target_author(target)
            report_count = Report.objects.filter(
                content_type_id=r.content_type_id, object_id=r.object_id).count()
            snap = _content_snapshot(target)
            results.append({
                "id": r.id, "reason": r.reason, "detail": r.detail,
                "content_type": _content_label(target),
                "content_title": snap["title"], "snippet": snap["snippet"],
                "reporter": author_badge(r.reporter),
                "author": author_badge(author) if author else None,
                "report_count": report_count,
                "resolved": r.resolved, "created_at": r.created_at,
            })
        return Response({"results": results, "count": total})


class ModReportDismissView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        report.resolved = True
        report.resolved_at = timezone.now()
        report.save(update_fields=["resolved", "resolved_at"])
        _log_action(request.user, ModerationAction.ACTION_DISMISS,
                    note=request.data.get("note", ""))
        return Response({"resolved": True})


class ModReportDeleteView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        target = report.target
        author = _target_author(target) if target else None
        # Log before removing — a hard-deleted Reply's pk goes to None
        # once .delete() runs, so the audit entry must capture it first.
        _log_action(request.user, ModerationAction.ACTION_DELETE, target_user=author,
                    target=target, note=request.data.get("note", ""))
        if target is not None:
            _remove_content(target)
        Report.objects.filter(
            content_type_id=report.content_type_id, object_id=report.object_id
        ).update(resolved=True, resolved_at=timezone.now())
        return Response({"deleted": True})


class ModReportWarnView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        author = _target_author(report.target) if report.target else None
        if author is None:
            return Response({"detail": "Could not resolve the content's author."},
                            status=status.HTTP_400_BAD_REQUEST)
        note = request.data.get("note", "")
        report.resolved = True
        report.resolved_at = timezone.now()
        report.save(update_fields=["resolved", "resolved_at"])
        _log_action(request.user, ModerationAction.ACTION_WARN, target_user=author, note=note)
        message = "You've received a formal warning from a ShikshaCom moderator." + (f" Note: {note}" if note else "")
        _notify_user(author, request.user, "forum.warned", message, note)
        return Response({"warned": True})


class ModReportBanView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        author = _target_author(report.target) if report.target else None
        if author is None:
            return Response({"detail": "Could not resolve the content's author."},
                            status=status.HTTP_400_BAD_REQUEST)
        note = request.data.get("note", "")
        _ban_user(author, note)
        report.resolved = True
        report.resolved_at = timezone.now()
        report.save(update_fields=["resolved", "resolved_at"])
        _log_action(request.user, ModerationAction.ACTION_BAN, target_user=author, note=note)
        message = "You have been banned from the ShikshaCom forum." + (f" Reason: {note}" if note else "")
        _notify_user(author, request.user, "forum.banned", message, note)
        return Response({"banned": True})


class ModReportSuspendView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        author = _target_author(report.target) if report.target else None
        if author is None:
            return Response({"detail": "Could not resolve the content's author."},
                            status=status.HTTP_400_BAD_REQUEST)
        note = request.data.get("note", "")
        duration_days = request.data.get("duration_days", 7)
        until = _suspend_user(author, duration_days, note)
        report.resolved = True
        report.resolved_at = timezone.now()
        report.save(update_fields=["resolved", "resolved_at"])
        _log_action(request.user, ModerationAction.ACTION_SUSPEND, target_user=author, note=note)
        message = f"You have been temporarily suspended from the ShikshaCom forum until {until:%d %b %Y}." + (f" Reason: {note}" if note else "")
        _notify_user(author, request.user, "forum.suspended", message, note)
        return Response({"suspended": True, "suspended_until": until})


class ModReportLockView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        thread = _target_thread(report.target) if report.target else None
        if thread is None:
            return Response({"detail": "Could not resolve the report's thread."},
                            status=status.HTTP_400_BAD_REQUEST)
        thread.is_locked = True
        thread.save(update_fields=["is_locked"])
        _log_action(request.user, ModerationAction.ACTION_LOCK, target=thread,
                    note=request.data.get("note", ""))
        return Response({"locked": True})


class ModReportUnlockView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, report_id):
        report = get_object_or_404(Report, pk=report_id)
        thread = _target_thread(report.target) if report.target else None
        if thread is None:
            return Response({"detail": "Could not resolve the report's thread."},
                            status=status.HTTP_400_BAD_REQUEST)
        thread.is_locked = False
        thread.save(update_fields=["is_locked"])
        _log_action(request.user, ModerationAction.ACTION_UNLOCK, target=thread,
                    note=request.data.get("note", ""))
        return Response({"locked": False})


# =====================================================
# Auto-Rejected Queue
# =====================================================
class ModAutoRejectedView(APIView):
    permission_classes = [IsForumModerator]

    def get(self, request):
        category = request.query_params.get("category")
        status_param = request.query_params.get("status", "pending")
        qs = AutoRejectedSubmission.objects.select_related("author", "thread").order_by("-created_at")
        if status_param != "all":
            qs = qs.filter(status=status_param)
        rows = list(qs)
        if category:
            rows = [s for s in rows if category in (s.categories or [])]

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 20, 100)
        total = len(rows)
        start = (page - 1) * page_size
        page_rows = rows[start:start + page_size]

        results = [{
            "id": s.id, "kind": s.kind, "title": s.title, "content": s.content,
            "tags": [t for t in s.tags.split(",") if t],
            "categories": [
                {"key": c, "label": forum_moderation.CATEGORY_LABELS.get(c, c)}
                for c in (s.categories or [])
            ],
            "author": author_badge(s.author),
            "thread_title": s.thread.title if s.thread_id else None,
            "status": s.status, "created_at": s.created_at,
        } for s in page_rows]
        return Response({"results": results, "count": total})


class ModAutoRejectedDeleteView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, submission_id):
        sub = get_object_or_404(AutoRejectedSubmission, pk=submission_id)
        sub.status = AutoRejectedSubmission.STATUS_DELETED
        sub.reviewed_by = request.user
        sub.reviewed_at = timezone.now()
        sub.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        _log_action(request.user, ModerationAction.ACTION_DELETE, target_user=sub.author,
                    target=sub, note=request.data.get("note", ""))
        return Response({"status": sub.status})


class ModAutoRejectedRestoreView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, submission_id):
        sub = get_object_or_404(AutoRejectedSubmission, pk=submission_id)
        if sub.kind in (ForumPost.KIND_QUESTION, ForumPost.KIND_POST):
            post = ForumPost.objects.create(
                author=sub.author, title=sub.title, content=sub.content, kind=sub.kind,
            )
            for name in [t.strip() for t in sub.tags.split(",") if t.strip()]:
                tag, _ = Tag.objects.get_or_create(name=name.lower())
                post.tags.add(tag)
            created_id = post.id
        else:
            if not sub.thread_id:
                return Response(
                    {"detail": "The original thread no longer exists."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            reply = Reply.objects.create(
                post=sub.thread, author=sub.author, content=sub.content, kind=sub.kind,
            )
            created_id = reply.id

        sub.status = AutoRejectedSubmission.STATUS_RESTORED
        sub.reviewed_by = request.user
        sub.reviewed_at = timezone.now()
        sub.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        _log_action(request.user, ModerationAction.ACTION_RESTORE, target_user=sub.author, target=sub)
        return Response({"status": sub.status, "created_id": created_id})


class ModAutoRejectedBanAuthorView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, submission_id):
        sub = get_object_or_404(AutoRejectedSubmission, pk=submission_id)
        note = request.data.get("note", "")
        _ban_user(sub.author, note)
        _log_action(request.user, ModerationAction.ACTION_BAN, target_user=sub.author, note=note)
        message = "You have been banned from the ShikshaCom forum." + (f" Reason: {note}" if note else "")
        _notify_user(sub.author, request.user, "forum.banned", message, note)
        return Response({"banned": True})


# =====================================================
# User Management
# =====================================================
def _user_forum_stats(user, post_ct, reply_ct):
    post_ids = list(ForumPost.objects.filter(author=user).values_list("id", flat=True))
    reply_ids = list(Reply.objects.filter(author=user).values_list("id", flat=True))
    posts_count = len(post_ids) + len(reply_ids)
    reports_count = Report.objects.filter(
        Q(content_type=post_ct, object_id__in=post_ids)
        | Q(content_type=reply_ct, object_id__in=reply_ids)
    ).count()
    profile = getattr(user, "forum_profile", None)
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
    return posts_count, reports_count, status_label, suspended_until


class ModUsersView(APIView):
    permission_classes = [IsForumModerator]

    def get(self, request):
        search = (request.query_params.get("search") or "").strip()
        status_filter = request.query_params.get("status", "all")

        post_ct = ContentType.objects.get_for_model(ForumPost)
        reply_ct = ContentType.objects.get_for_model(Reply)

        candidate_ids = (
            set(ForumPost.objects.values_list("author_id", flat=True))
            | set(Reply.objects.values_list("author_id", flat=True))
            | set(ForumProfile.objects.values_list("user_id", flat=True))
        )
        qs = User.objects.filter(id__in=candidate_ids).select_related("forum_profile")
        if search:
            qs = qs.filter(Q(username__icontains=search) | Q(email__icontains=search))
        qs = qs.order_by("username")

        enriched = []
        for u in qs:
            posts_count, reports_count, status_label, suspended_until = _user_forum_stats(u, post_ct, reply_ct)
            if status_filter != "all" and status_label != status_filter:
                continue
            enriched.append({
                "id": str(u.id), "username": u.username, "email": u.email,
                "initials": author_badge(u)["initials"], "color": author_badge(u)["color"],
                "posts": posts_count, "reports": reports_count, "status": status_label,
                "suspended_until": suspended_until,
            })

        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 20, 100)
        total = len(enriched)
        start = (page - 1) * page_size
        return Response({"results": enriched[start:start + page_size], "count": total})


class ModUserWarnView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        note = request.data.get("note", "")
        _log_action(request.user, ModerationAction.ACTION_WARN, target_user=user, note=note)
        message = "You've received a formal warning from a ShikshaCom moderator." + (f" Note: {note}" if note else "")
        _notify_user(user, request.user, "forum.warned", message, note)
        return Response({"warned": True})


class ModUserBanView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        note = request.data.get("note", "")
        _ban_user(user, note)
        _log_action(request.user, ModerationAction.ACTION_BAN, target_user=user, note=note)
        message = "You have been banned from the ShikshaCom forum." + (f" Reason: {note}" if note else "")
        _notify_user(user, request.user, "forum.banned", message, note)
        return Response({"banned": True})


class ModUserSuspendView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        note = request.data.get("note", "")
        duration_days = request.data.get("duration_days", 7)
        until = _suspend_user(user, duration_days, note)
        _log_action(request.user, ModerationAction.ACTION_SUSPEND, target_user=user, note=note)
        message = f"You have been temporarily suspended from the ShikshaCom forum until {until:%d %b %Y}." + (f" Reason: {note}" if note else "")
        _notify_user(user, request.user, "forum.suspended", message, note)
        return Response({"suspended": True, "suspended_until": until})


class ModUserUnbanView(APIView):
    """Reinstates a user regardless of whether they were banned or
    suspended — the moderator UI shows this as a single "Reinstate" action."""
    permission_classes = [IsForumModerator]

    def post(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        _reinstate_user(user)
        _log_action(request.user, ModerationAction.ACTION_UNBAN, target_user=user,
                    note=request.data.get("note", ""))
        message = "Your forum access has been fully restored. You can participate again."
        _notify_user(user, request.user, "forum.unbanned", message)
        return Response({"banned": False})


# =====================================================
# Thread Management ("All Threads" tab — lock/unlock, soft delete/restore)
# =====================================================
class ModThreadsView(APIView):
    """Distinct from the public forum-threads/ endpoint: this one includes
    removed and locked threads, since a moderator needs to see (and
    restore) them."""
    permission_classes = [IsForumModerator]

    def get(self, request):
        qs = _annotated_threads(request.user, include_removed=True).order_by("-created_at")
        page = _int_param(request, "page", 1, 100000)
        page_size = _int_param(request, "page_size", 20, 100)
        total = qs.count()
        start = (page - 1) * page_size
        rows = qs[start:start + page_size]
        results = [{
            "id": t.id, "title": t.title, "author": t.author.username,
            "reply_count": t.reply_count, "upvote_count": t.upvote_count,
            "created_at": t.created_at, "is_locked": t.is_locked, "is_removed": t.is_removed,
        } for t in rows]
        return Response({"results": results, "count": total})


class ModThreadLockView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, thread_id):
        thread = get_object_or_404(ForumPost, pk=thread_id)
        thread.is_locked = True
        thread.save(update_fields=["is_locked"])
        _log_action(request.user, ModerationAction.ACTION_LOCK, target=thread,
                    note=request.data.get("note", ""))
        return Response({"locked": True})


class ModThreadUnlockView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, thread_id):
        thread = get_object_or_404(ForumPost, pk=thread_id)
        thread.is_locked = False
        thread.save(update_fields=["is_locked"])
        _log_action(request.user, ModerationAction.ACTION_UNLOCK, target=thread,
                    note=request.data.get("note", ""))
        return Response({"locked": False})


class ModThreadDeleteView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, thread_id):
        thread = get_object_or_404(ForumPost, pk=thread_id)
        note = request.data.get("note", "")
        _log_action(request.user, ModerationAction.ACTION_DELETE, target_user=thread.author,
                    target=thread, note=note)
        thread.is_removed = True
        thread.removed_at = timezone.now()
        thread.save(update_fields=["is_removed", "removed_at"])
        return Response({"deleted": True})


class ModThreadRestoreView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, thread_id):
        thread = get_object_or_404(ForumPost, pk=thread_id)
        thread.is_removed = False
        thread.removed_at = None
        thread.save(update_fields=["is_removed", "removed_at"])
        _log_action(request.user, ModerationAction.ACTION_RESTORE, target_user=thread.author,
                    target=thread, note=request.data.get("note", ""))
        return Response({"restored": True})


# =====================================================
# Categories
# =====================================================
class ModCategoriesView(APIView):
    """List (including inactive) and create forum categories."""
    permission_classes = [IsForumModerator]

    def get(self, request):
        cats = ForumCategory.objects.all()
        return Response({
            "results": ForumCategorySerializer(cats, many=True, context={"request": request}).data,
            "count": cats.count(),
        })

    def post(self, request):
        serializer = CategoryWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        slug = data.get("slug")
        if slug and ForumCategory.objects.filter(slug=slug).exists():
            return Response({"detail": "A category with that slug already exists."},
                            status=status.HTTP_400_BAD_REQUEST)
        category = ForumCategory.objects.create(**data)
        return Response(
            ForumCategorySerializer(category, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ModCategoryUpdateView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, category_id):
        category = get_object_or_404(ForumCategory, slug=category_id)
        serializer = CategoryWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        slug = data.get("slug")
        if slug and ForumCategory.objects.filter(slug=slug).exclude(pk=category.pk).exists():
            return Response({"detail": "A category with that slug already exists."},
                            status=status.HTTP_400_BAD_REQUEST)
        for field, value in data.items():
            setattr(category, field, value)
        category.save()
        return Response(ForumCategorySerializer(category, context={"request": request}).data)


class ModCategoryDeleteView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, category_id):
        category = get_object_or_404(ForumCategory, slug=category_id)
        category.is_active = False
        category.save(update_fields=["is_active"])
        _log_action(request.user, ModerationAction.ACTION_DELETE, target=category,
                    note=request.data.get("note", ""))
        return Response({"deleted": True})


class ModCategoryRestoreView(APIView):
    permission_classes = [IsForumModerator]

    def post(self, request, category_id):
        category = get_object_or_404(ForumCategory, slug=category_id)
        category.is_active = True
        category.save(update_fields=["is_active"])
        _log_action(request.user, ModerationAction.ACTION_RESTORE, target=category,
                    note=request.data.get("note", ""))
        return Response({"restored": True})


# =====================================================
# Activity Log
# =====================================================
_LOG_META = {
    ModerationAction.ACTION_DISMISS: ("ok", "Dismissed"),
    ModerationAction.ACTION_DELETE: ("bad", "Removed"),
    ModerationAction.ACTION_WARN: ("warn", "Warned"),
    ModerationAction.ACTION_BAN: ("bad", "Banned"),
    ModerationAction.ACTION_UNBAN: ("ok", "Reinstated"),
    ModerationAction.ACTION_RESTORE: ("ok", "Restored"),
    ModerationAction.ACTION_SUSPEND: ("warn", "Suspended"),
    ModerationAction.ACTION_LOCK: ("warn", "Locked"),
    ModerationAction.ACTION_UNLOCK: ("ok", "Unlocked"),
}


def _log_row_text(a, target):
    label = _LOG_META.get(a.action, ("ok", a.get_action_display()))[1]
    title = getattr(target, "title", None) if target is not None else None
    if title:
        return f"{label} thread “{title}”"
    if a.target_user:
        return f"{label} {a.target_user.username}"
    return label


class ModLogView(APIView):
    """Full paginated audit trail — the Activity Log tab. Distinct from
    ModAnalyticsView's `recent_actions`, which is a fixed 10-row summary."""
    permission_classes = [IsForumModerator]

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
            results.append({
                "id": a.id, "action": a.action, "type": action_type, "label": label,
                "text": _log_row_text(a, target), "note": a.note,
                "moderator": a.moderator.username if a.moderator else "—",
                "target_user": a.target_user.username if a.target_user else None,
                "created_at": a.created_at,
            })
        return Response({"results": results, "count": total})


# =====================================================
# Analytics
# =====================================================
class ModAnalyticsView(APIView):
    permission_classes = [IsForumModerator]

    def get(self, request):
        now = timezone.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_month_end = month_start - timedelta(seconds=1)
        prev_month_start = prev_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)

        def pct_change(cur, prev):
            if not prev:
                return None
            return round((cur - prev) / prev * 100, 1)

        reports_this_month = Report.objects.filter(created_at__gte=month_start).count()
        reports_prev_month = Report.objects.filter(
            created_at__gte=prev_month_start, created_at__lt=month_start).count()

        resolution_seconds = [
            (r.resolved_at - r.created_at).total_seconds()
            for r in Report.objects.filter(
                resolved=True, resolved_at__isnull=False, resolved_at__gte=month_start)
        ]
        avg_resolution_hours = (
            sum(resolution_seconds) / len(resolution_seconds) / 3600
        ) if resolution_seconds else 0

        active_users_7d = User.objects.filter(
            Q(forum_posts__created_at__gte=week_ago) | Q(forum_replies__created_at__gte=week_ago)
        ).distinct().count()

        new_posts_7d = ForumPost.objects.filter(created_at__gte=week_ago).count()

        approved_30d = ForumPost.objects.filter(created_at__gte=month_ago).count()
        rejected_30d = AutoRejectedSubmission.objects.filter(created_at__gte=month_ago).count()
        approval_rate = (
            approved_30d / (approved_30d + rejected_30d) * 100
        ) if (approved_30d + rejected_30d) else 100

        banned_this_month = ModerationAction.objects.filter(
            action=ModerationAction.ACTION_BAN, created_at__gte=month_start).count()
        banned_prev_month = ModerationAction.objects.filter(
            action=ModerationAction.ACTION_BAN,
            created_at__gte=prev_month_start, created_at__lt=month_start).count()

        kpis = [
            {"label": "Total reports this month", "value": reports_this_month,
             "trend": pct_change(reports_this_month, reports_prev_month), "direction": "bad_if_up"},
            {"label": "Avg. resolution time (hrs)", "value": round(avg_resolution_hours, 1),
             "trend": None, "direction": "good_if_down"},
            {"label": "Active users (7d)", "value": active_users_7d,
             "trend": None, "direction": "good_if_up"},
            {"label": "New posts (7d)", "value": new_posts_7d,
             "trend": None, "direction": "good_if_up"},
            {"label": "Posts approved (%)", "value": round(approval_rate, 1),
             "trend": None, "direction": "neutral"},
            {"label": "Banned this month", "value": banned_this_month,
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
            "posts_approved": ForumPost.objects.filter(created_at__gte=month_start).count(),
            "users_warned": ModerationAction.objects.filter(
                action=ModerationAction.ACTION_WARN, created_at__gte=month_start).count(),
            "users_banned": banned_this_month,
        }

        # Header stat cards, shown above the tab bar regardless of active tab.
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        pending_reports = Report.objects.filter(resolved=False)
        header_stats = {
            "open_reports": pending_reports.count(),
            "high_priority": pending_reports.filter(reason__in=HIGH_SEVERITY_REASONS).count(),
            "banned_users": ForumProfile.objects.filter(is_banned=True).count(),
            "actions_today": ModerationAction.objects.filter(created_at__gte=today_start).count(),
        }

        return Response({
            "kpis": kpis,
            "reports_by_reason": reports_by_reason,
            "recent_actions": recent_actions,
            "this_month": this_month,
            "header_stats": header_stats,
        })


# ─────────────────────────────────────────────────────────────────────────
# Admin Moderator Activity overview (is_staff) — backs the admin console's
# "Moderator Activity" screen. Distinct from the moderator-gated ModAnalyticsView
# above: this is oversight OF moderators, gated on is_staff.
# ─────────────────────────────────────────────────────────────────────────
def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


class AdminModerationOverviewView(APIView):
    """GET /forum/admin/moderation-overview/?range=7d
    → { kpis, moderators[], breakdown[], queues[] }"""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        raw = (request.query_params.get("range") or "7d").strip().lower()
        try:
            days = max(1, min(int(raw.rstrip("d")), 365))
        except (TypeError, ValueError):
            days = 7
        since = timezone.now() - timedelta(days=days)

        # ── Actions in range ──
        actions_qs = ModerationAction.objects.filter(created_at__gte=since)
        actions_count = actions_qs.count()

        # ── Escalations (bans + suspends) ──
        escalations = actions_qs.filter(
            action__in=[ModerationAction.ACTION_BAN, ModerationAction.ACTION_SUSPEND]
        ).count()

        # ── Median response time on resolved forum reports (minutes) ──
        resolved_reports = Report.objects.filter(
            resolved=True, resolved_at__isnull=False, created_at__gte=since
        ).values_list("created_at", "resolved_at")
        deltas = [(r - c).total_seconds() / 60.0 for c, r in resolved_reports]
        median_response = _median(deltas)

        # ── Open reports (forum + chat) ──
        forum_open = Report.objects.filter(resolved=False).count()
        chat_open = 0
        try:
            from chat.models import Report as ChatReport
            chat_open = ChatReport.objects.filter(status="OPEN").count()
        except Exception:
            pass
        pending_review = AutoRejectedSubmission.objects.filter(
            status=AutoRejectedSubmission.STATUS_PENDING
        ).count()

        kpis = [
            {"key": "actions", "label": f"Actions · {days}d", "value": actions_count},
            {"key": "median_response", "label": "Median response",
             "value": (f"{round(median_response)} min" if median_response is not None else "—")},
            {"key": "escalations", "label": "Escalations", "value": escalations},
            {"key": "reports_open", "label": "Reports open", "value": forum_open + chat_open},
        ]

        # ── Per-moderator rows ──
        by_mod = (
            actions_qs.values("moderator")
            .annotate(n=Count("id"))
            .order_by("-n")[:50]
        )
        mod_ids = [r["moderator"] for r in by_mod if r["moderator"]]
        users = {u.id: u for u in User.objects.filter(id__in=mod_ids)}
        moderators = []
        for r in by_mod:
            u = users.get(r["moderator"])
            if not u:
                continue
            moderators.append({
                "name": (u.get_full_name() or "").strip() or u.username or u.email,
                "email": u.email,
                "week": r["n"],
            })

        # ── Action-type breakdown ──
        breakdown = [
            {"type": r["action"], "count": r["n"]}
            for r in actions_qs.values("action").annotate(n=Count("id")).order_by("-n")
        ]

        # ── Queues ──
        queues = [
            {"key": "chat", "label": "Chat reports", "count": chat_open, "href": "/communication/reports"},
            {"key": "forum", "label": "Forum reports open", "count": forum_open, "href": "/roles"},
            {"key": "review", "label": "Posts pending review", "count": pending_review, "href": "/roles"},
        ]

        return Response({
            "range_days": days,
            "kpis": kpis,
            "moderators": moderators,
            "breakdown": breakdown,
            "queues": queues,
        })
