"""Admin Teacher Activity — unified academy-teaching + skill-session feed.

The `activity.Activity` model is an audience-scoped notification feed, not a
teacher-action audit log, so KPIs and the feed are aggregated from the source
models directly (live classes, chapter coverage, uploads, quizzes, skill
sessions). Cross-app imports are wrapped defensively (mirrors AdminStatsView).
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin


def range_start(request, default_days=7):
    """Parse ?range=7d|30d|90d into a timezone-aware start datetime."""
    raw = (request.query_params.get("range") or f"{default_days}d").strip().lower()
    try:
        days = int(raw.rstrip("d"))
    except (TypeError, ValueError):
        days = default_days
    days = max(1, min(days, 365))
    return timezone.now() - timedelta(days=days), days


def _name(user):
    if not user:
        return "—"
    try:
        lp = user.default_learner_profile()
        if lp and getattr(lp, "full_name", ""):
            return lp.full_name
    except Exception:
        pass
    return (user.get_full_name() or "").strip() or user.username or user.email


class AdminTeacherActivityView(APIView):
    """GET /activity/admin/teacher-activity/?range=7d → {kpis, feed}."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        since, days = range_start(request)
        kpis = {
            "live_classes": 0,
            "chapters_covered": 0,
            "skill_sessions": 0,
            "skill_amount": 0,   # paise, gross
            "uploads": 0,
            "quizzes": 0,
        }
        feed = []

        # ── Live classes ──
        try:
            from livestream.models import LiveSession
            live = (
                LiveSession.objects.filter(start_time__gte=since)
                .exclude(status=LiveSession.STATUS_CANCELLED)
                .select_related("subject", "created_by")
                .order_by("-start_time")
            )
            kpis["live_classes"] = live.count()
            for s in live[:20]:
                feed.append({
                    "type": "live",
                    "teacher": _name(s.created_by),
                    "text": f"Live class · {s.title}",
                    "when": (s.actual_started_at or s.start_time).isoformat() if (s.actual_started_at or s.start_time) else None,
                })
        except Exception:
            pass

        # ── Chapter coverage ──
        try:
            from courses.models_batch_progress import BatchChapterProgress
            cov = (
                BatchChapterProgress.objects.filter(is_covered=True, covered_at__gte=since)
                .select_related("chapter", "batch", "marked_by")
                .order_by("-covered_at")
            )
            kpis["chapters_covered"] = cov.count()
            for c in cov[:20]:
                feed.append({
                    "type": "coverage",
                    "teacher": _name(c.marked_by),
                    "text": f"Covered {getattr(c.chapter, 'title', getattr(c.chapter, 'name', 'a chapter'))}",
                    "when": c.covered_at.isoformat() if c.covered_at else None,
                })
        except Exception:
            pass

        # ── Uploads (materials + recordings) ──
        try:
            from materials.models import StudyMaterial
            mats = (
                StudyMaterial.objects.filter(created_at__gte=since)
                .select_related("uploaded_by")
                .order_by("-created_at")
            )
            kpis["uploads"] += mats.count()
            for m in mats[:15]:
                feed.append({
                    "type": "upload",
                    "teacher": _name(m.uploaded_by),
                    "text": f"Uploaded material · {m.title}",
                    "when": m.created_at.isoformat() if m.created_at else None,
                })
        except Exception:
            pass
        try:
            from courses.models_recordings import SessionRecording
            recs = (
                SessionRecording.objects.filter(created_at__gte=since)
                .select_related("uploaded_by")
                .order_by("-created_at")
            )
            kpis["uploads"] += recs.count()
            for r in recs[:15]:
                feed.append({
                    "type": "upload",
                    "teacher": _name(r.uploaded_by),
                    "text": f"Uploaded recording · {r.title}",
                    "when": r.created_at.isoformat() if r.created_at else None,
                })
        except Exception:
            pass

        # ── Quizzes ──
        try:
            from quizzes.models import Quiz
            quizzes = (
                Quiz.objects.filter(created_at__gte=since)
                .select_related("created_by")
                .order_by("-created_at")
            )
            kpis["quizzes"] = quizzes.count()
            for qz in quizzes[:15]:
                feed.append({
                    "type": "quiz",
                    "teacher": _name(qz.created_by),
                    "text": f"Created quiz · {qz.title}",
                    "when": qz.created_at.isoformat() if qz.created_at else None,
                })
        except Exception:
            pass

        # ── Skill sessions ──
        try:
            from django.db.models import Sum
            from skills.models import SkillSession
            skill = (
                SkillSession.objects.filter(created_at__gte=since, status=SkillSession.STATUS_COMPLETED)
                .select_related("expert", "expert__teacher_profile", "expert__teacher_profile__user")
                .order_by("-created_at")
            )
            kpis["skill_sessions"] = skill.count()
            agg = skill.aggregate(total=Sum("amount"))
            kpis["skill_amount"] = agg["total"] or 0
            for ss in skill[:15]:
                tp = getattr(ss.expert, "teacher_profile", None)
                feed.append({
                    "type": "skill",
                    "teacher": _name(getattr(tp, "user", None)),
                    "text": "Completed a skill session",
                    "when": ss.created_at.isoformat() if ss.created_at else None,
                })
        except Exception:
            pass

        # Merge feed, newest first, cap 50.
        feed = [f for f in feed if f.get("when")]
        feed.sort(key=lambda f: f["when"], reverse=True)
        feed = feed[:50]

        return Response({"range_days": days, "kpis": kpis, "feed": feed})
