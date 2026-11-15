"""GET /api/dashboard/admin/search/?q= — one box across the whole console.

design_handoff_admin_counsellor, Admin v2 Phase 5. The artboard's top bar
reads "Find a student, course, teacher or page" — none of which the existing
search could find.

WHAT WAS ALREADY THERE, AND WHY IT WASN'T ENOUGH
────────────────────────────────────────────────
`content.StudioSearchView` (GET /content/admin/search/) is a good search over
posts, answers, pages, labels and pictures — but that is the CMS only. It
also sits behind `IsStudioEditor`, which 403s whenever
`content_studio_enabled` is off, so the ⌘K palette was mounted only for
Studio admins.

This view keeps that content half (by delegating, not reimplementing — one
definition of "find a post") and adds the people and catalogue halves the
artboard asks for.

⚠ DELEGATION, NOT DUPLICATION. The content results come from
`StudioSearchView`'s own queryset code. A second implementation would drift,
and the CMS half is the part most likely to grow new types.

⚠ The content half is SKIPPED, not failed, when the Studio flag is off —
`IsStudioEditor` would refuse it. The response says so in `skipped` rather
than silently returning fewer results, because "no posts matched" and "posts
were not searched" are different answers.
"""
import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin

logger = logging.getLogger(__name__)

MIN_QUERY = 2
PER_KIND = 5


def _people(q):
    """Students, teachers and staff — by name or email.

    ⚠ `display_name` lives on LearnerProfile, not User, and a User may have
    several profiles. Searching only User.email would miss anyone whose name
    is the thing you remember; searching only profiles would miss accounts
    with no profile. Both, de-duplicated on the account.
    """
    from django.db.models import Q

    from accounts.models import User

    out, seen = [], set()
    qs = (User.objects
          .filter(Q(email__icontains=q)
                  | Q(username__icontains=q)
                  | Q(learner_profiles__display_name__icontains=q))
          .distinct()[:PER_KIND])
    for u in qs:
        if u.pk in seen:
            continue
        seen.add(u.pk)
        roles = u.get_active_roles() if hasattr(u, "get_active_roles") else []
        label = "Teacher" if "TEACHER" in roles else (
            "Admin" if "ADMIN" in roles else "Student")
        out.append({
            "kind": "person", "kind_label": label,
            "title": (u.learner_profiles.first().display_name
                      if u.learner_profiles.exists() else None) or u.email or u.username,
            "where": u.email or u.username,
            # Users.jsx owns the per-account detail view.
            "url": f"/users/{u.pk}",
        })
    return out


def _courses(q):
    from courses.models import Course

    out = []
    for c in Course.objects.filter(title__icontains=q)[:PER_KIND]:
        out.append({
            "kind": "course", "kind_label": "Course",
            "title": c.title,
            # Status matters here: a DRAFT course is invisible to visitors,
            # and that is the single most common "why isn't this live" question.
            "where": (c.status or "").title() or "Course",
            "url": "/courses",
        })
    return out


def _content(request, q):
    """Delegate to the CMS search rather than re-querying its models."""
    from content.studio_views import StudioSearchView

    view = StudioSearchView()
    view.request = request
    return view.get(request).data.get("results", [])


class AdminSearchView(APIView):
    """GET /api/dashboard/admin/search/?q=

        {"query": "...", "results": [{kind, kind_label, title, where, url}],
         "skipped": ["content"]}

    One flat ranked list, matching what the palette renders — not per-type
    sections. `url` is an ADMIN-APP route, not an API path; the palette
    navigates straight to it.
    """

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        if len(q) < MIN_QUERY:
            return Response({"query": q, "results": [], "skipped": []})

        results, skipped = [], []

        # People and courses first: the artboard's own example query is a
        # student's name, and a name match is almost always what was meant.
        for name, fn in (("person", _people), ("course", _courses)):
            try:
                results.extend(fn(q))
            except Exception:
                logger.exception("admin search: %s failed", name)
                skipped.append(name)

        from content.permissions import IsStudioEditor
        if IsStudioEditor().has_permission(request, self):
            try:
                results.extend(_content(request, q))
            except Exception:
                logger.exception("admin search: content failed")
                skipped.append("content")
        else:
            # Not an error — the CMS search is gated and the flag is off.
            skipped.append("content")

        return Response({"query": q, "results": results, "skipped": skipped})
