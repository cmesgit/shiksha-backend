"""Admin Analytics — insights across enrollments, revenue, and engagement.

dashboard/ has no models of its own, so everything is aggregated from other
apps. Cross-app access is wrapped defensively (mirrors accounts.AdminStatsView)
so a missing/empty app degrades to zeros instead of a 500.
"""
from datetime import timedelta

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin


def _range(request, default_days=30):
    raw = (request.query_params.get("range") or f"{default_days}d").strip().lower()
    try:
        days = max(1, min(int(raw.rstrip("d")), 365))
    except (TypeError, ValueError):
        days = default_days
    return timezone.now() - timedelta(days=days), days


def _daily_series(qs, date_field, value=None):
    """Bucket a queryset by day. value=None → row count; else Sum(value)."""
    agg = Count("id") if value is None else Sum(value)
    rows = (
        qs.annotate(_d=TruncDate(date_field))
        .values("_d")
        .annotate(v=agg)
        .order_by("_d")
    )
    return [
        {"date": r["_d"].isoformat() if r["_d"] else None, "value": r["v"] or 0}
        for r in rows
    ]


class AdminAnalyticsView(APIView):
    """GET /dashboard/admin/analytics/?range=30d&metric=enrollments|revenue|engagement
    → { range_days, metric, kpis, series, breakdowns }"""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        since, days = _range(request)
        metric = (request.query_params.get("metric") or "enrollments").strip().lower()

        kpis = []
        series = []
        breakdowns = []

        # ── KPIs (headline, resilient) ──
        try:
            from enrollments.models import Enrollment
            active = Enrollment.objects.filter(status=Enrollment.STATUS_ACTIVE).count()
            new_in_range = Enrollment.objects.filter(enrolled_at__gte=since).count()
            kpis.append({"key": "active_enrollments", "label": "Active enrollments", "value": active})
            kpis.append({"key": "new_enrollments", "label": f"New · {days}d", "value": new_in_range})
        except Exception:
            pass
        try:
            from payments.models import Order
            revenue = (
                Order.objects.filter(status=Order.STATUS_PAID, created_at__gte=since)
                .aggregate(t=Sum("amount"))["t"]
            ) or 0
            kpis.append({"key": "revenue", "label": f"Revenue · {days}d", "value": revenue, "format": "currency"})
        except Exception:
            pass
        try:
            from livestream.models import LiveSession
            live = LiveSession.objects.filter(
                start_time__gte=since, status=LiveSession.STATUS_COMPLETED
            ).count()
            kpis.append({"key": "live_classes", "label": f"Live classes · {days}d", "value": live})
        except Exception:
            pass

        # ── Series + breakdowns by metric ──
        if metric == "revenue":
            try:
                from payments.models import Order
                paid = Order.objects.filter(status=Order.STATUS_PAID, created_at__gte=since)
                series = [{"label": "Revenue (paise)", "points": _daily_series(paid, "created_at", value="amount")}]
                breakdowns = [
                    {"label": r["course__title"] or "—", "value": r["v"] or 0}
                    for r in paid.values("course__title").annotate(v=Sum("amount")).order_by("-v")[:8]
                ]
            except Exception:
                pass
        elif metric == "engagement":
            try:
                from livestream.models import LiveSessionAttendance
                att = LiveSessionAttendance.objects.filter(joined_at__gte=since)
                series = [{"label": "Class joins", "points": _daily_series(att, "joined_at")}]
            except Exception:
                pass
            try:
                from forum.models import ForumPost
                posts = ForumPost.objects.filter(created_at__gte=since)
                series.append({"label": "Forum posts", "points": _daily_series(posts, "created_at")})
            except Exception:
                pass
        else:  # enrollments (default)
            try:
                from enrollments.models import Enrollment
                enr = Enrollment.objects.filter(enrolled_at__gte=since)
                series = [{"label": "New enrollments", "points": _daily_series(enr, "enrolled_at")}]
                breakdowns = [
                    {"label": r["course__title"] or "—", "value": r["v"]}
                    for r in enr.values("course__title").annotate(v=Count("id")).order_by("-v")[:8]
                ]
            except Exception:
                pass

        return Response({
            "range_days": days,
            "metric": metric,
            "kpis": kpis,
            "series": series,
            "breakdowns": breakdowns,
        })
