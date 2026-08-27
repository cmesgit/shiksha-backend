"""Content Studio API — history, per-author drafts, and publishing.

design_handoff_content_studio Phase 1b. Kept out of ``admin_views.py`` because
that file is already 460 lines of straightforward CRUD and none of this is
CRUD: these are the workflow endpoints the split page editor needs.

Mounted under the app's existing prefix, so the real paths are
``/api/content/admin/…`` — NOT ``/admin/content/…`` as the handoff spec and all
five of its scaffolds assume.

A "page" is not a model. The homepage is a set of ``HomeContentBlock`` rows
keyed by ``HomeSection``, so a page-level draft is really one ``ContentDraft``
per section row per author, aggregated on read. ``PAGES`` below is the only
place that mapping lives.
"""
from datetime import datetime, timedelta

from django.core.exceptions import FieldDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from django.contrib.contenttypes.models import ContentType

from .admin_serializers import HomeContentBlockAdminSerializer
from .models import (
    LIST_CONTENT_ELSEWHERE, SECTIONS_WITH_LIST_ITEMS,
    ContentDraft, ContentRevision, HomeContentBlock, HomeSection,
    HomeSectionOrder, PublishStatus,
)
from .permissions import IsContentEditor
from .revisions import record_revision, restore_revision, snapshot_of

# ── The page registry ─────────────────────────────────────────────
# Only the homepage today. Other pages follow the same shape: a section enum
# plus the model whose rows carry each section's copy.
PAGES = {
    "home": {
        "label": "Home page",
        "url": "/",
        "sections": HomeSection,
        "model": HomeContentBlock,
    },
}


def _page_or_404(key):
    page = PAGES.get(key)
    if page is None:
        from django.http import Http404
        raise Http404(f"No page named {key!r}")
    return page


# `section` is the row's identity, not its content, and it is UNIQUE. A draft
# that sets it passes the whitelist, then raises IntegrityError inside the
# atomic publish — and because the rollback takes `draft.delete()` with it, the
# draft survives and every later publish fails identically. The page becomes
# permanently unpublishable until someone deletes the row by hand. Excluded
# explicitly rather than inherited from the CRUD serializer, which has every
# reason to expose `section` and no reason to know about drafts.
_DRAFT_PROTECTED_FIELDS = {"section"}


def _editable_fields(model):
    """Field names a draft payload may set.

    Whitelisted from the admin serializer rather than the model, so a draft can
    never write a field the admin API itself refuses to expose (ids, timestamps,
    and anything deliberately read-only).
    """
    ser = HomeContentBlockAdminSerializer()
    return {
        name for name, field in ser.fields.items()
        if not field.read_only and hasattr(model, name)
        and name not in _DRAFT_PROTECTED_FIELDS
    }


def _field_errors(instance, values):
    """Validate draft values against the model's own field validators.

    Returns ``{field: message}`` for anything that would not survive a save.

    This runs on both the draft PUT and the publish. Without it an over-length
    or malformed value is accepted happily, then reaches ``block.save()``:
    sqlite shrugs, but prod is Postgres, which raises ``DataError`` from inside
    the atomic publish. Same permanent-lock consequence as a bad `section`
    above, so the value is refused at the point the editor can still see which
    field they broke.
    """
    errors = {}
    for name, value in values.items():
        try:
            field = instance._meta.get_field(name)
        except FieldDoesNotExist:
            continue
        try:
            field.clean(value, instance)
        except DjangoValidationError as exc:
            errors[name] = " ".join(exc.messages)
    return errors


class StudioPagination(PageNumberPagination):
    page_size = 30
    page_size_query_param = "page_size"
    max_page_size = 100


# ── What the inbox and calendar look at ───────────────────────────
# Only two models carry `publish_at` (they inherit PublishableModel), so only
# those two can be *scheduled*. The six StatusedContentModel models have status
# but no publish time — they can be drafts or in review, never "publishing on
# Tuesday". Keeping that distinction explicit here stops the calendar from
# quietly pretending a homepage section has a publish date.
STALE_DRAFT_DAYS = 7


def _schedulable():
    from .models import BlogPost, CurrentAffair
    return [
        (BlogPost, "post", "Post", lambda o: f"/content/blogs/{o.id}"),
        (CurrentAffair, "affair", "Current affair", lambda o: "/content?tab=affairs"),
    ]


def _reviewable():
    """Everything that can sit in draft or review, schedulable or not."""
    from .models import (
        Announcement, BlogPost, CurrentAffair, FAQItem, HomeContentBlock,
        HomeFloater, HomeListItem, ShowcaseCourse,
    )
    return [
        (BlogPost, "post", "Post", lambda o: f"/content/blogs/{o.id}", "title"),
        (CurrentAffair, "affair", "Current affair", lambda o: "/content?tab=affairs", "title"),
        (FAQItem, "answer", "Answer", lambda o: "/content/questions", "question"),
        (Announcement, "notice", "Notice", lambda o: "/content/questions?tab=notices", "message"),
        # /content?tab=showcase is not in ContentPanel's tab list and not in its
        # redirect map either, so it silently fell through to Blog Posts.
        (ShowcaseCourse, "card", "Course card", lambda o: "/content/cards", "title"),
        (HomeContentBlock, "page", "Page section", lambda o: "/content/pages/home", "heading"),
        (HomeListItem, "page", "Page list item", lambda o: "/content/pages/home", "title"),
        (HomeFloater, "page", "Page floater", lambda o: "/content?tab=home", "label"),
    ]


def _title_of(obj, field):
    return (getattr(obj, field, "") or "").strip() or str(obj)[:80]


class InboxView(APIView):
    """GET /api/content/admin/inbox/ — the home screen's "Needs you" card.

    Three sources, each a different kind of waiting work:
      * publishing_today — scheduled to go live before midnight
      * awaiting_you     — sitting in review
      * stale_drafts     — a draft nobody has touched in a week

    Every item carries a deep link, because a to-do you cannot click is just
    a reminder.
    """

    permission_classes = [IsContentEditor]

    def get(self, request):
        now = timezone.now()
        local = timezone.localtime(now)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        stale_before = now - timedelta(days=STALE_DRAFT_DAYS)

        publishing_today = []
        for model, kind, label, link in _schedulable():
            for obj in model.objects.filter(
                status=PublishStatus.PUBLISHED,
                publish_at__gte=day_start, publish_at__lt=day_end,
            )[:10]:
                publishing_today.append({
                    "kind": kind, "kind_label": label,
                    "title": _title_of(obj, "title"),
                    "reason": f"Goes live at {timezone.localtime(obj.publish_at):%-I:%M %p}",
                    "state": "scheduled",
                    "url": link(obj),
                    "at": obj.publish_at,
                })

        awaiting_you, stale_drafts = [], []
        for model, kind, label, link, title_field in _reviewable():
            for obj in model.objects.filter(status=PublishStatus.REVIEW)[:10]:
                awaiting_you.append({
                    "kind": kind, "kind_label": label,
                    "title": _title_of(obj, title_field),
                    "reason": "Someone asked you to look at this",
                    "state": "review",
                    "url": link(obj),
                    "at": obj.updated_at,
                })
            for obj in model.objects.filter(
                status=PublishStatus.DRAFT, updated_at__lt=stale_before,
            )[:10]:
                days = (now - obj.updated_at).days
                stale_drafts.append({
                    "kind": kind, "kind_label": label,
                    "title": _title_of(obj, title_field),
                    "reason": f"Draft, untouched for {days} days",
                    "state": "stale",
                    "url": link(obj),
                    "at": obj.updated_at,
                })

        groups = [
            {"key": "publishing_today", "label": "Publishing today", "items": publishing_today},
            {"key": "awaiting_you", "label": "Someone asked you", "items": awaiting_you},
            {"key": "stale_drafts", "label": "Forgotten drafts", "items": stale_drafts},
        ]
        return Response({
            "groups": groups,
            "total": sum(len(g["items"]) for g in groups),
            "stale_after_days": STALE_DRAFT_DAYS,
        })


class CalendarView(APIView):
    """GET /api/content/admin/calendar/?from=&to= — the "This week" grid.

    Only the two schedulable models appear. A date with nothing on it still
    comes back, so the client renders seven cells without inventing the gaps.
    """

    permission_classes = [IsContentEditor]

    def get(self, request):
        today = timezone.localtime(timezone.now()).date()
        start = _parse_date(request.query_params.get("from")) or (
            today - timedelta(days=today.weekday())
        )
        end = _parse_date(request.query_params.get("to")) or (start + timedelta(days=6))
        if end < start:
            start, end = end, start
        # A runaway range would scan the whole table; a quarter is plenty for
        # any calendar the screen can draw.
        end = min(end, start + timedelta(days=92))

        by_day = {
            (start + timedelta(days=i)).isoformat(): []
            for i in range((end - start).days + 1)
        }

        for model, kind, label, link in _schedulable():
            for obj in model.objects.filter(
                publish_at__date__gte=start, publish_at__date__lte=end,
            )[:200]:
                day = timezone.localtime(obj.publish_at).date().isoformat()
                if day in by_day:
                    by_day[day].append({
                        "kind": kind, "kind_label": label,
                        "title": _title_of(obj, "title"),
                        "status": obj.status,
                        "url": link(obj),
                        "at": obj.publish_at,
                    })

        return Response({
            "from": start.isoformat(),
            "to": end.isoformat(),
            "today": today.isoformat(),
            "days": [
                {"date": d, "items": by_day[d]} for d in sorted(by_day)
            ],
        })


def _parse_date(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


# ── ⌘K search ─────────────────────────────────────────────────────

class StudioSearchView(APIView):
    """GET /api/content/admin/search/?q= — one box across every content type.

    Returns a flat list of ``{kind, title, where, url}`` because the palette
    renders one ranked list, not per-type sections. ``url`` is an admin-app
    route, not an API path — the palette navigates straight there.

    Deliberately spans two Django apps: labels are ``content.ContentTag`` AND
    ``courses.CourseCategory``, which is the merge the Labels screen makes
    visible in Phase 7. The category half is what puts the seven competitive
    exams in the navbar, so it belongs in a CMS search box.
    """

    permission_classes = [IsContentEditor]
    PER_KIND = 5

    def get(self, request):
        from courses.models import CourseCategory

        from .models import BlogPost, ContentImage, ContentTag, FAQItem

        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Response({"results": [], "query": q})

        results = []

        for post in BlogPost.objects.filter(title__icontains=q)[:self.PER_KIND]:
            results.append({
                "kind": "post", "kind_label": "Post",
                "title": post.title,
                "where": f"/{post.slug}",
                "url": f"/content/blogs/{post.id}",
                "status": post.status,
            })

        for faq in FAQItem.objects.filter(question__icontains=q)[:self.PER_KIND]:
            results.append({
                "kind": "answer", "kind_label": "Answer",
                "title": faq.question,
                # Several FAQPage labels already end in "page" ("General / FAQ
                # page"), so appending it unconditionally reads "… page page".
                "where": faq.get_page_display(),
                "url": "/content/questions",
                "status": faq.status,
            })

        for block in HomeContentBlock.objects.filter(
            heading__icontains=q
        )[:self.PER_KIND]:
            results.append({
                "kind": "page", "kind_label": "Page section",
                "title": block.heading or block.get_section_display(),
                "where": f"Home page · {block.get_section_display()}",
                "url": "/content/pages/home",
                "status": block.status,
            })

        for tag in ContentTag.objects.filter(name__icontains=q)[:self.PER_KIND]:
            results.append({
                "kind": "label", "kind_label": "Label",
                "title": tag.name,
                "where": "Blog tag",
                "url": "/content/labels",
            })

        for cat in CourseCategory.objects.filter(name__icontains=q)[:self.PER_KIND]:
            results.append({
                "kind": "label", "kind_label": "Label",
                "title": cat.name,
                "where": f"Course category · {cat.get_group_display()}",
                "url": "/content/labels",
            })

        for img in ContentImage.objects.filter(
            Q(original_name__icontains=q) | Q(title__icontains=q)
            | Q(alt_text__icontains=q) | Q(file__icontains=q)
        )[:self.PER_KIND]:
            results.append({
                "kind": "picture", "kind_label": "Picture",
                "title": img.title or img.file.name.rsplit("/", 1)[-1],
                "where": f"{img.width or '?'} × {img.height or '?'}",
                "url": "/content/pictures",
            })

        return Response({"results": results, "query": q, "count": len(results)})


# ── Competitive exam readiness ────────────────────────────────────
#
# ⚠ An exam is a COURSE, not a board. Two representations exist:
# `Course.kind="COACHING"` + a CourseCategory whose group is "competitive"
# (real), and `Board.board_type=COMPETITIVE` (zero rows, dead capability).
# This builds on the first.
#
# ⚠ The competitive check is `content.admin_serializers._is_competitive`,
# reused rather than reimplemented. It tests BOTH signals because they can
# disagree — `create_competitive_courses` skips the category link with a
# warning when categories were never seeded, yielding a COACHING course with
# no group. Keying on either alone misses exactly the misfiled ones.

# The pipeline the screen draws. Each step is "done" purely from a count, so
# the screen can never claim progress the data doesn't support.
EXAM_STEPS = ["has_card", "subject_count", "chapter_count", "material_count", "quiz_count"]
EXAM_STEP_LABELS = ["Card", "Subjects", "Chapters", "Material", "Tests"]


class ExamReadinessView(APIView):
    """GET /api/content/admin/exams/readiness/

    How far each competitive exam has actually got. Every number is a real
    count — if it says zero subjects, there are zero subjects.
    """

    permission_classes = [IsContentEditor]

    def get(self, request):
        from django.db.models import Count

        from courses.models import Course

        from .admin_serializers import _is_competitive
        from .models import ShowcaseCourse

        # Widen, then filter with the shared check — `kind` alone would miss a
        # course linked only by category, and vice versa.
        candidates = (
            Course.objects.filter(
                Q(kind="COACHING") | Q(categories__group="competitive")
            )
            .distinct()
            .prefetch_related("categories")
            .annotate(
                n_subjects=Count("subjects", distinct=True),
                n_chapters=Count("subjects__chapters", distinct=True),
            )
        )

        carded = set(
            ShowcaseCourse.objects.filter(course__isnull=False)
            .values_list("course_id", flat=True)
        )

        course_ids = [c.id for c in candidates]

        # Materials and quizzes live in other apps, so they can't join into the
        # annotate() above without multiplying its rows. Counting them per
        # course would be an N+1 — two extra queries per exam. One grouped
        # query each instead, regardless of how many exams there are.
        from materials.models import StudyMaterial
        from quizzes.models import Quiz

        materials_by_course = dict(
            StudyMaterial.objects
            .filter(subject__course_id__in=course_ids)
            .values_list("subject__course_id")
            .annotate(n=Count("id"))
        )
        quizzes_by_course = dict(
            Quiz.objects
            .filter(subject__course_id__in=course_ids)
            .values_list("subject__course_id")
            .annotate(n=Count("id"))
        )

        # `_is_competitive` queries `course.categories` when `kind` isn't
        # COACHING, which is another per-exam query. Resolve that half here in
        # one go; the helper stays the authority for the rest and short-circuits
        # on `kind` without touching the database.
        competitive_ids = set(
            Course.objects.filter(
                id__in=course_ids, categories__group="competitive",
            ).values_list("id", flat=True)
        )

        exams = []
        for course in candidates:
            if course.id not in competitive_ids and not _is_competitive(course):
                continue

            material_count = materials_by_course.get(course.id, 0)
            quiz_count = quizzes_by_course.get(course.id, 0)

            # ⚠ A DRAFT or ARCHIVED course is not in the navbar, whatever its
            # kind says. Prod carries two such rows ("hy", a stray "NEET"), and
            # counting them as live made the screen claim nine exams were
            # published when seven were. Reporting it beats deleting the rows:
            # both turned out to own real related data (a Batch, a CourseDetail).
            published = course.status in (
                Course.STATUS_PUBLISHED, Course.STATUS_COMING_SOON,
            )

            counts = {
                "has_card": 1 if course.id in carded else 0,
                "subject_count": course.n_subjects,
                "chapter_count": course.n_chapters,
                "material_count": material_count,
                "quiz_count": quiz_count,
            }
            # Derived server-side, never stored: a stored flag would drift the
            # moment someone added a subject through the course editor.
            state = (
                "live"
                if counts["subject_count"] > 0 and counts["material_count"] > 0
                else "coming_soon"
            )
            exams.append({
                "id": str(course.id),
                "course_status": course.status,
                "in_navbar": published,
                "slug": getattr(course, "slug", ""),
                "name": course.title,
                # Course.description, not short_description — the latter does
                # not exist, so getattr's default made every blurb blank while
                # the screen looked fine. (`blurb` belongs to CourseCategory.)
                "blurb": (getattr(course, "description", "") or "").strip()[:160],
                **counts,
                "steps": [
                    {"key": k, "label": lbl, "done": counts[k] > 0,
                     "count": counts[k]}
                    for k, lbl in zip(EXAM_STEPS, EXAM_STEP_LABELS)
                ],
                "state": state,
                "edit_url": f"/courses?course={course.id}",
            })

        exams.sort(key=lambda e: (-sum(1 for s in e["steps"] if s["done"]), e["name"]))

        live = sum(1 for e in exams if e["state"] == "live")
        in_navbar = sum(1 for e in exams if e["in_navbar"])
        hidden = [e for e in exams if not e["in_navbar"]]
        # The one worth finishing first: furthest along but not yet live.
        suggested = next(
            (e["id"] for e in exams if e["state"] != "live" and e["in_navbar"]),
            None,
        )

        return Response({
            "exams": exams,
            "pipeline": EXAM_STEP_LABELS,
            "summary": {
                "total": len(exams),
                "in_navbar": in_navbar,
                "not_published": len(hidden),
                "with_subjects": sum(1 for e in exams if e["subject_count"] > 0),
                # Scoped to what a visitor can actually reach. Counting the
                # unpublished rows here said "12 showing Coming soon" while
                # only 10 were on the site.
                "coming_soon": sum(
                    1 for e in exams if e["in_navbar"] and e["state"] != "live"
                ),
                "live": live,
            },
            "suggested_id": suggested,
            # Quiz scheduling does not exist — Quiz has no start/availability
            # date at all. The setup rail marks that step blocked rather than
            # inviting someone to do something impossible.
            "scheduling_available": False,
        })


# ── Labels: tags and course categories on one screen ──────────────
#
# ⚠ The SCREEN is merged, not the tables. `content.ContentTag` and
# `courses.CourseCategory` stay separate models, for two independent reasons:
# merging them would touch the public blog filters, the navbar and the catalog
# for a cosmetic win, and they live in different Django apps, so no single
# migration owns both.
#
# ⚠ `CourseCategory.group` is load-bearing. The `competitive` group is what
# puts the seven competitive exams in the navbar and on /courses. Merging or
# deleting one silently un-lists every exam, so both operations guard on it.

LABEL_TAG = "tag"
LABEL_CATEGORY = "category"


def _normalise(name):
    return " ".join((name or "").split()).casefold()


def _tag_usage(tag):
    return tag.blog_posts.count() + tag.current_affairs.count()


def _label_rows():
    """Both kinds, annotated with how often each is actually used."""
    from courses.models import CourseCategory

    from .models import ContentTag

    rows = []
    for tag in ContentTag.objects.prefetch_related("blog_posts", "current_affairs"):
        rows.append({
            "id": tag.id, "kind": LABEL_TAG, "kind_label": "Blog tag",
            "name": tag.name, "slug": tag.slug,
            "usage_count": _tag_usage(tag),
            "usage_label": "posts and articles",
            "group": None,
        })
    for cat in CourseCategory.objects.prefetch_related("courses"):
        rows.append({
            "id": cat.id, "kind": LABEL_CATEGORY, "kind_label": "Course category",
            "name": cat.name, "slug": cat.slug,
            "usage_count": cat.courses.count(),
            "usage_label": "courses",
            "group": cat.group,
            "group_label": cat.get_group_display(),
        })
    return rows


def _mark_duplicates(rows):
    """Duplicate detection is a query, not a stored field.

    Two labels look like duplicates when their names match once case and
    surrounding whitespace are ignored, or when their slugs match. Only labels
    of the same kind are ever compared — a blog tag and a course category
    sharing a name are not duplicates, they are two different things.
    """
    by_key = {}
    for row in rows:
        key = (row["kind"], _normalise(row["name"]))
        by_key.setdefault(key, []).append(row)
        slug_key = (row["kind"], "slug:" + (row["slug"] or "").casefold())
        by_key.setdefault(slug_key, []).append(row)

    for group in by_key.values():
        seen = {r["id"]: r for r in group}
        if len(seen) < 2:
            continue
        # The most-used one is the sensible merge target, so it is the one
        # everything else is reported as a duplicate OF.
        ordered = sorted(seen.values(), key=lambda r: (-r["usage_count"], r["id"]))
        keeper = ordered[0]
        for other in ordered[1:]:
            other["duplicate_of"] = {"id": keeper["id"], "name": keeper["name"]}
    return rows


class LabelListView(APIView):
    """GET /api/content/admin/labels/ — tags and categories in one list."""

    permission_classes = [IsContentEditor]

    def get(self, request):
        rows = _mark_duplicates(_label_rows())

        q = (request.query_params.get("q") or "").strip().casefold()
        if q:
            rows = [r for r in rows if q in r["name"].casefold()]

        rows.sort(key=lambda r: (r["kind"], r["name"].casefold()))
        return Response({
            "results": rows,
            "count": len(rows),
            "duplicate_count": sum(1 for r in rows if r.get("duplicate_of")),
        })

    def post(self, request):
        """Create a label of either kind.

        Closes the last thing `Tags.jsx` / `Categories.jsx` could still do that
        this screen could not, which is what keeps those two screens alive.
        """
        from courses.models import CourseCategory

        from .models import ContentTag

        kind = request.data.get("kind")
        name = (request.data.get("name") or "").strip()
        if kind not in (LABEL_TAG, LABEL_CATEGORY):
            return Response(
                {"detail": "kind must be 'tag' or 'category'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not name:
            return Response(
                {"detail": "Give the label a name."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if kind == LABEL_TAG:
            # ContentTag.slug is unique and slugified from the name, so a
            # case/whitespace variant would raise IntegrityError. Say so in
            # words instead of letting a 500 through.
            existing = next(
                (t for t in ContentTag.objects.all()
                 if _normalise(t.name) == _normalise(name)),
                None,
            )
            if existing is not None:
                return Response(
                    {
                        "detail": (
                            f"“{existing.name}” already exists — the same label "
                            "with different capitalisation is the same label."
                        ),
                        "existing_id": existing.id,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            obj = ContentTag.objects.create(name=name)
            payload = {"id": obj.id, "kind": LABEL_TAG, "name": obj.name}
        else:
            group = request.data.get("group")
            valid = {g for g, _ in CourseCategory.GROUP_CHOICES}
            if group not in valid:
                return Response(
                    {"detail": f"group must be one of: {', '.join(sorted(valid))}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            obj = CourseCategory.objects.create(name=name, group=group)
            payload = {
                "id": obj.id, "kind": LABEL_CATEGORY, "name": obj.name,
                "group": obj.group,
            }

        return Response(payload, status=status.HTTP_201_CREATED)


class LabelMergeView(APIView):
    """POST /api/content/admin/labels/merge/ — {from_id, into_id, kind}

    Repoints every relation inside one transaction, then deletes the source.
    Renaming and merging are the only safe operations here; nothing is ever
    left half-pointed.
    """

    permission_classes = [IsContentEditor]

    @transaction.atomic
    def post(self, request):
        from courses.models import CourseCategory

        from .models import ContentTag

        kind = request.data.get("kind")
        from_id = request.data.get("from_id")
        into_id = request.data.get("into_id")

        if kind not in (LABEL_TAG, LABEL_CATEGORY):
            return Response(
                {"detail": "kind must be 'tag' or 'category'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if from_id is None or into_id is None:
            return Response(
                {"detail": "from_id and into_id are both required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if str(from_id) == str(into_id):
            return Response(
                {"detail": "A label can’t be merged into itself."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        model = ContentTag if kind == LABEL_TAG else CourseCategory
        source = model.objects.filter(pk=from_id).first()
        target = model.objects.filter(pk=into_id).first()
        if source is None or target is None:
            return Response(
                {"detail": "One of those labels no longer exists."},
                status=status.HTTP_404_NOT_FOUND,
            )

        moved = 0
        if kind == LABEL_TAG:
            for post in source.blog_posts.all():
                post.tags.add(target)
                post.tags.remove(source)
                moved += 1
            for affair in source.current_affairs.all():
                affair.tags.add(target)
                affair.tags.remove(source)
                moved += 1
        else:
            # ⚠ Merging across groups would move a course out of the group its
            # discovery depends on — most sharply for `competitive`, which is
            # what lists the seven exams at all.
            if source.group != target.group:
                return Response(
                    {
                        "detail": (
                            f"“{source.name}” is in {source.get_group_display()} and "
                            f"“{target.name}” is in {target.get_group_display()}. "
                            "Merging across groups would move its courses out of "
                            "the section visitors browse them in."
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            for course in source.courses.all():
                course.categories.add(target)
                course.categories.remove(source)
                moved += 1

        name = source.name
        source.delete()
        return Response({
            "detail": f"Merged “{name}” into “{target.name}”.",
            "moved": moved,
            "into": {"id": target.id, "name": target.name},
        })


class LabelDetailView(APIView):
    """PATCH (rename) and DELETE one label."""

    permission_classes = [IsContentEditor]

    def _resolve(self, kind, pk):
        from courses.models import CourseCategory

        from .models import ContentTag

        # Unlike the create and merge views, this one used to accept any string
        # as `kind` and silently treat everything that wasn't "tag" as a
        # category — so /labels/banana/1/ happily renamed a CourseCategory.
        if kind not in (LABEL_TAG, LABEL_CATEGORY):
            return None
        model = ContentTag if kind == LABEL_TAG else CourseCategory
        return model.objects.filter(pk=pk).first()

    def patch(self, request, kind, pk):
        obj = self._resolve(kind, pk)
        if obj is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response(
                {"detail": "Give the label a name."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The create path already answers a duplicate with a friendly 409; the
        # rename path used to let the UNIQUE constraint surface as a 500.
        # Checked only where the column really is unique — `ContentTag.name` is,
        # `CourseCategory.name` is not (its save() appends "-2" instead).
        if type(obj)._meta.get_field("name").unique:
            clash = (
                type(obj).objects
                .filter(name__iexact=name).exclude(pk=obj.pk).first()
            )
            if clash is not None:
                return Response(
                    {
                        "detail": (
                            f"“{clash.name}” already exists. Rename this one "
                            "something else, or merge the two labels."
                        ),
                        "existing": {"id": clash.id, "name": clash.name},
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        obj.name = name
        obj.save()
        return Response({"id": obj.id, "name": obj.name, "slug": obj.slug})

    def delete(self, request, kind, pk):
        obj = self._resolve(kind, pk)
        if obj is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if kind == LABEL_CATEGORY:
            from courses.models import CourseCategory

            used = obj.courses.count()
            last_in_group = (
                CourseCategory.objects.filter(group=obj.group).count() == 1
            )
            # Deleting the last competitive category doesn't just orphan its
            # courses — it removes the group that makes them discoverable.
            if last_in_group and used:
                return Response(
                    {
                        "detail": (
                            f"“{obj.name}” is the only category in "
                            f"{obj.get_group_display()}, and "
                            + (
                                "1 course relies on it. Deleting it would "
                                "remove it from the site’s browsing."
                                if used == 1 else
                                f"{used} courses rely on it. Deleting it "
                                "would remove them from the site’s browsing."
                            )
                        ),
                        "used_by": used,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
        else:
            used = _tag_usage(obj)

        if used:
            return Response(
                {
                    "detail": (
                        f"“{obj.name}” is still used {used} time"
                        f"{'' if used == 1 else 's'}. Merge it into another "
                        "label instead, so nothing loses its label."
                    ),
                    "used_by": used,
                },
                status=status.HTTP_409_CONFLICT,
            )

        name = obj.name
        obj.delete()
        return Response({"detail": f"Deleted “{name}”."})


# ── Publish checklist ─────────────────────────────────────────────
# Levels: "block" cannot be published past, "warn" publishes behind a confirm,
# "ok" is informational. Every check is phrased as what a reader would notice,
# not as a field constraint — "The heading is empty, so this section has no
# title on the page" rather than "heading: required".
HEADING_MIN, HEADING_MAX = 12, 70


def _checklist_for_block(block, draft_payload):
    """Run the checks against the DRAFT view of a section, not the live row.

    Checking the live row would pass a section whose pending edit empties the
    heading, which is exactly the mistake the checklist exists to catch.
    """
    def value(field):
        if draft_payload and field in draft_payload:
            return draft_payload[field]
        return getattr(block, field, "") or ""

    checks = []
    heading = str(value("heading")).strip()
    if not heading:
        checks.append({
            "id": "heading", "level": "block",
            "label": "This section has no heading",
            "note": "Visitors would see the section with no title on it.",
        })
    elif len(heading) > HEADING_MAX:
        checks.append({
            "id": "heading", "level": "warn",
            "label": "The heading is quite long",
            "note": f"{len(heading)} characters. Long headings wrap awkwardly on a phone.",
        })
    elif len(heading) < HEADING_MIN:
        checks.append({
            "id": "heading", "level": "warn",
            "label": "The heading is very short",
            "note": f"{len(heading)} characters.",
        })
    else:
        checks.append({
            "id": "heading", "level": "ok",
            "label": "The heading reads well", "note": "",
        })

    # A button with words but nowhere to go is the single most common way a
    # homepage edit ships broken, so it blocks rather than warns.
    for which, label_field, href_field in (
        ("main", "cta_primary_label", "cta_primary_href"),
        ("second", "cta_secondary_label", "cta_secondary_href"),
    ):
        text = str(value(label_field)).strip()
        href = str(value(href_field)).strip()
        if text and not href:
            checks.append({
                "id": f"cta_{which}", "level": "block",
                "label": f"The {which} button has no destination",
                "note": f"“{text}” would do nothing when clicked.",
            })
        elif href and not text:
            checks.append({
                "id": f"cta_{which}", "level": "warn",
                "label": f"The {which} button has a destination but no words",
                "note": "It will not appear on the page.",
            })

    has_picture = bool(value("image") or str(value("image_url")).strip())
    if has_picture:
        checks.append({
            "id": "picture", "level": "ok",
            "label": "The picture is set", "note": "",
        })

    if block.status != PublishStatus.PUBLISHED:
        checks.append({
            "id": "visibility", "level": "warn",
            "label": "This section is hidden from visitors",
            "note": "Publishing saves your changes but the section still won’t show.",
        })

    return checks


class PageChecklistView(APIView):
    """GET /api/content/admin/pages/<key>/checklist/

    Everything the Publish button needs to decide whether it can be pressed.
    Runs over this author's draft, section by section.
    """

    permission_classes = [IsContentEditor]

    def get(self, request, key):
        page = _page_or_404(key)
        model = page["model"]
        ct = ContentType.objects.get_for_model(model)
        blocks = {b.section: b for b in model.objects.all()}
        drafts = {
            d.object_id: d.payload for d in ContentDraft.objects.filter(
                content_type=ct,
                object_id__in=[b.id for b in blocks.values()],
                author=request.user,
            )
        }

        sections, blocking, warning = [], 0, 0
        for value_, label in page["sections"].choices:
            block = blocks.get(value_)
            if block is None:
                continue
            payload = drafts.get(block.id)
            # Only check sections the author has actually touched — otherwise
            # a pre-existing problem elsewhere on the page blocks an unrelated
            # edit, and the button can never be pressed.
            if not payload:
                continue
            checks = _checklist_for_block(block, payload)
            blocking += sum(1 for c in checks if c["level"] == "block")
            warning += sum(1 for c in checks if c["level"] == "warn")
            sections.append({"key": value_, "label": label, "checks": checks})

        return Response({
            "sections": sections,
            "blocking": blocking,
            "warnings": warning,
            "can_publish": blocking == 0 and bool(sections),
            "nothing_to_publish": not sections,
        })


class LinkTargetsView(APIView):
    """GET /api/content/admin/link-targets/

    Real destinations for a button, so the editor offers a dropdown of pages
    instead of a URL box nobody can fill in correctly. A hand-typed href is
    how a homepage button ends up pointing at a 404.
    """

    permission_classes = [IsContentEditor]

    def get(self, request):
        groups = [{
            "label": "Site pages",
            "options": [
                {"value": "/", "label": "Home"},
                {"value": "/courses", "label": "All courses"},
                {"value": "/about", "label": "About"},
                {"value": "/contact", "label": "Contact"},
                {"value": "/blogs", "label": "Blog"},
                {"value": "/live", "label": "Live sessions"},
            ],
        }]

        try:
            from courses.models import CourseCategory
            cats = [
                {"value": f"/courses?category={c.slug}", "label": c.name}
                for c in CourseCategory.objects.filter(is_active=True)[:40]
            ]
            if cats:
                groups.append({"label": "Course categories", "options": cats})
        except Exception:  # noqa: BLE001 — a missing catalog must not break the editor
            pass

        return Response({"groups": groups})


# ── Media library ─────────────────────────────────────────────────

class MediaListView(APIView):
    """GET/POST /api/content/admin/media/ — the Pictures screen.

    The list carries ``usage_count`` and ``used_in[]``, which is the whole
    point of the screen: before this, nobody could tell where a picture was
    used, or whether deleting it would blank a live page.

    ⚠ This does NOT replace ``admin/editor-images``. That route still resolves
    and the blog block editor still uploads through it — they are two views
    over one ``ContentImage`` table, not two libraries.
    """

    permission_classes = [IsContentEditor]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        from .media import usage_payload
        from .models import ContentImage

        # `usages__target` prefetches the GenericForeignKey too. Without it
        # usage_payload's `usage.target` was one query per usage, and its
        # `.select_related("content_type")` built a fresh queryset that ignored
        # the prefetch cache entirely — a strict +2 queries per asset, ~403 on a
        # full page of the library.
        qs = ContentImage.objects.all().prefetch_related(
            "usages__content_type", "usages__target"
        ).order_by("-created_at")

        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(original_name__icontains=q) | Q(title__icontains=q)
                | Q(alt_text__icontains=q) | Q(file__icontains=q)
            )

        # `count` used to be len(results) after a hard qs[:200] slice, so the
        # library presented itself as complete at exactly 200 and everything
        # past that was unreachable and undeletable through the UI.
        total = qs.count()

        def _int(name, default, cap):
            try:
                return max(1, min(int(request.query_params.get(name) or default), cap))
            except (TypeError, ValueError):
                return default

        page_size = _int("page_size", 60, 200)
        page = _int("page", 1, 10_000)
        start = (page - 1) * page_size

        assets = []
        for asset in qs[start:start + page_size]:
            used_in = usage_payload(asset)
            assets.append({
                "id": asset.id,
                "url": asset.file.url if asset.file else "",
                "name": asset.original_name or (
                    asset.file.name.rsplit("/", 1)[-1] if asset.file else ""
                ),
                "alt_text": asset.alt_text,
                "width": asset.width,
                "height": asset.height,
                "created_at": asset.created_at,
                "usage_count": len(used_in),
                "used_in": used_in,
            })
        return Response({
            "results": assets,
            "count": total,
            "page": page,
            "page_size": page_size,
            "has_more": start + len(assets) < total,
        })

    def post(self, request):
        from .models import ContentImage

        upload = request.FILES.get("file")
        if not upload:
            return Response(
                {"detail": "Choose a picture to upload."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        asset = ContentImage(
            file=upload,
            original_name=(getattr(upload, "name", "") or "")[:200],
            alt_text=request.data.get("alt_text", "") or "",
            uploaded_by=request.user if request.user.is_authenticated else None,
        )
        try:
            asset.full_clean(exclude=["uploaded_by"])
        except DjangoValidationError as exc:
            return Response(
                {"detail": "; ".join(sum(exc.message_dict.values(), []))},
                status=status.HTTP_400_BAD_REQUEST,
            )
        asset.save()
        return Response(
            {
                "id": asset.id,
                "url": asset.file.url,
                "name": asset.original_name,
                "width": asset.width,
                "height": asset.height,
                "usage_count": 0,
                "used_in": [],
            },
            status=status.HTTP_201_CREATED,
        )


class MediaDetailView(APIView):
    """DELETE /api/content/admin/media/<id>/ — refused while in use.

    409 with ``used_in[]`` rather than a bare error, so the screen can name the
    pages that would break and offer to open the first one.
    """

    permission_classes = [IsContentEditor]

    def delete(self, request, pk):
        from .media import usage_payload
        from .models import ContentImage

        asset = get_object_or_404(ContentImage, pk=pk)
        used_in = usage_payload(asset)
        if used_in:
            where = used_in[0]["title"]
            more = len(used_in) - 1
            return Response(
                {
                    "detail": (
                        f"This picture is still used on “{where}”"
                        + (f" and {more} other place{'' if more == 1 else 's'}" if more else "")
                        + ". Remove it there first."
                    ),
                    "used_in": used_in,
                },
                status=status.HTTP_409_CONFLICT,
            )
        asset.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── History ───────────────────────────────────────────────────────

class ActivityFeedView(APIView):
    """GET /api/content/admin/activity/ — the History screen's feed.

    Grouped by day, because that is how the screen renders it; doing the
    grouping here keeps the client from having to re-derive local dates from
    timestamps and getting the boundaries wrong.
    """

    permission_classes = [IsContentEditor]

    def get(self, request):
        try:
            limit = min(int(request.query_params.get("limit", 50)), 200)
        except (TypeError, ValueError):
            limit = 50

        revisions = (
            ContentRevision.objects
            .select_related("content_type", "actor")
            .all()[:limit]
        )

        groups, order = {}, []
        for rev in revisions:
            local = timezone.localtime(rev.created_at)
            day = local.date().isoformat()
            if day not in groups:
                groups[day] = []
                order.append(day)
            groups[day].append({
                "id": rev.id,
                "action": rev.action,
                "note": rev.note,
                "kind": rev.content_type.model,
                "kind_label": rev.content_type.name,
                "object_id": rev.object_id,
                "actor": (
                    rev.actor.get_full_name() or rev.actor.email
                ) if rev.actor else None,
                "actor_id": rev.actor_id,
                "at": rev.created_at,
                # The feed offers Undo on every row; restoring a snapshot of a
                # row that has since been deleted is a no-op, so say so here
                # rather than letting the click fail.
                "can_restore": bool(rev.snapshot),
            })

        return Response({
            "days": [{"date": d, "items": groups[d]} for d in order],
            "count": len(revisions),
        })


class RevisionRestoreView(APIView):
    """POST /api/content/admin/revisions/<id>/restore/ — the feed's Undo.

    Restoring records a further revision rather than deleting one, so undo of
    an undo works and history is never destroyed.
    """

    permission_classes = [IsContentEditor]

    def post(self, request, pk):
        revision = get_object_or_404(ContentRevision, pk=pk)
        obj = restore_revision(revision, actor=request.user)
        if obj is None:
            return Response(
                {"detail": "The item this change belonged to no longer exists."},
                status=status.HTTP_410_GONE,
            )
        return Response({
            "detail": "Restored.",
            "object_id": obj.pk,
            "kind": revision.content_type.model,
        })


# ── Drafts ────────────────────────────────────────────────────────

class PageDraftView(APIView):
    """GET/PUT /api/content/admin/pages/<key>/draft/

    The draft is per author. Two editors on the homepage keep separate pending
    changes instead of silently overwriting one another.
    """

    permission_classes = [IsContentEditor]

    def _blocks(self, page):
        return {b.section: b for b in page["model"].objects.all()}

    def _drafts(self, page, user):
        ct = ContentType.objects.get_for_model(page["model"])
        rows = ContentDraft.objects.filter(
            content_type=ct,
            object_id__in=page["model"].objects.values_list("id", flat=True),
            author=user,
        )
        by_object = {d.object_id: d for d in rows}
        return ct, by_object

    def get(self, request, key):
        page = _page_or_404(key)
        blocks = self._blocks(page)
        _, drafts = self._drafts(page, request.user)
        order = {
            o.section: o for o in HomeSectionOrder.objects.all()
        } if page["model"] is HomeContentBlock else {}

        # Once, not once per section — it instantiates a serializer, and the
        # loop below runs for all 17 sections on every request.
        editable = sorted(_editable_fields(page["model"]))
        sections, payload, changed = [], {}, 0
        for value, label in page["sections"].choices:
            block = blocks.get(value)
            draft = drafts.get(block.id) if block else None
            dirty = sorted((draft.payload or {}).keys()) if draft else []
            changed += len(dirty)
            if dirty:
                payload[value] = draft.payload
            o = order.get(value)
            # The live field values, so the editor can show what is currently
            # on the page under any pending edit. Without these the fields
            # column has nothing to render and every input looks empty.
            values = {}
            if block is not None:
                for name in editable:
                    raw = getattr(block, name, "")
                    # dict/list pass through intact. Flattening them to "" meant
                    # a client that round-tripped what it was handed wrote a
                    # string into `extra`, a JSONField(default=dict).
                    if raw is None:
                        values[name] = ""
                    elif isinstance(raw, (str, int, bool, dict, list)):
                        values[name] = raw
                    else:
                        values[name] = raw.name if hasattr(raw, "name") else ""
                values["img"] = block.image.url if block.image else (
                    block.image_url or ""
                )

            sections.append({
                "key": value,
                "label": label,
                "has_content": block is not None,
                "status": block.status if block else None,
                "values": values,
                # Drives the section list's amber edited-dot. Derived from the
                # draft's keys, never stored as a flag.
                "edited_fields": dirty,
                "order": o.order if o else None,
                "is_visible": o.is_visible if o else True,
                # Whether the public component for this section actually
                # renders HomeListItem rows. The editor offered the list panel
                # on every section, so rows saved against a section that
                # ignores them were invisible on the site forever.
                "supports_list_items": value in SECTIONS_WITH_LIST_ITEMS,
                # …and where that content really lives, for the two sections
                # whose repeatable content is a different model on another
                # screen (cards from courses, questions from answers).
                "list_source": LIST_CONTENT_ELSEWHERE.get(value),
            })

        return Response({
            "page": {"key": key, "label": page["label"], "url": page["url"]},
            # Sorted the way visitors actually see the page, not in enum
            # declaration order. The section list's own footnote promises
            # "the order here is the order visitors see", and it has to be
            # true before drag-to-reorder can mean anything. Sections with no
            # HomeSectionOrder row have no place on the page yet, so they sort
            # last, keeping their declaration order among themselves.
            "sections": sorted(
                sections,
                key=lambda s: (s["order"] is None, s["order"] or 0),
            ),
            "draft": payload,
            "change_count": changed,
        })

    @transaction.atomic
    def put(self, request, key):
        page = _page_or_404(key)
        blocks = self._blocks(page)
        ct = ContentType.objects.get_for_model(page["model"])
        allowed = _editable_fields(page["model"])

        incoming = request.data.get("sections")
        if not isinstance(incoming, dict):
            return Response(
                {"detail": "Expected an object under 'sections'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        rejected, saved = {}, 0
        for section_key, fields in incoming.items():
            block = blocks.get(section_key)
            if block is None:
                rejected[section_key] = "no content block for this section"
                continue
            if not isinstance(fields, dict):
                rejected[section_key] = "expected an object of field values"
                continue

            clean = {k: v for k, v in fields.items() if k in allowed}
            bad = sorted(set(fields) - allowed)
            if bad:
                rejected[section_key] = f"not editable: {', '.join(bad)}"

            # Refuse a value the column could never hold, here rather than at
            # publish — the editor is still looking at the field they broke.
            invalid = _field_errors(block, clean)
            if invalid:
                detail = "; ".join(f"{k}: {v}" for k, v in sorted(invalid.items()))
                rejected[section_key] = (
                    f"{rejected[section_key]}; {detail}"
                    if section_key in rejected else detail
                )
                clean = {k: v for k, v in clean.items() if k not in invalid}

            draft, _ = ContentDraft.objects.get_or_create(
                content_type=ct, object_id=block.id, author=request.user,
                defaults={"payload": {}},
            )
            merged = dict(draft.payload or {})
            merged.update(clean)
            # A field edited back to the live value stops being an edit —
            # otherwise the "unpublished edits" count never returns to zero.
            live = snapshot_of(block)
            merged = {
                k: v for k, v in merged.items()
                if k not in live or live[k] != v
            }
            if merged:
                draft.payload = merged
                draft.save(update_fields=["payload", "updated_at"])
                saved += len(merged)
            else:
                draft.delete()

        body = self.get(request, key).data
        body["saved_fields"] = saved
        if rejected:
            body["rejected"] = rejected
        return Response(body)

    @transaction.atomic
    def delete(self, request, key):
        """Discard this author's pending edits for the page."""
        page = _page_or_404(key)
        ct = ContentType.objects.get_for_model(page["model"])
        deleted, _ = ContentDraft.objects.filter(
            content_type=ct,
            object_id__in=page["model"].objects.values_list("id", flat=True),
            author=request.user,
        ).delete()
        return Response({"detail": "Pending edits discarded.", "deleted": deleted})


class PagePublishView(APIView):
    """POST /api/content/admin/pages/<key>/publish/

    Applies this author's drafts onto the live rows inside one transaction and
    deletes them. The payload holds only changed fields, so a field someone
    else edited meanwhile survives instead of being reverted by a stale copy.
    """

    permission_classes = [IsContentEditor]

    @transaction.atomic
    def post(self, request, key):
        page = _page_or_404(key)
        ct = ContentType.objects.get_for_model(page["model"])
        blocks = {b.id: b for b in page["model"].objects.all()}
        drafts = list(
            ContentDraft.objects
            .select_for_update()
            .filter(content_type=ct, object_id__in=blocks.keys(), author=request.user)
        )

        if not drafts:
            return Response(
                {"detail": "There are no unpublished edits to publish."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Run the checklist before touching anything. A blocking failure means
        # publishing would put something visibly broken on the live site, so
        # the write never starts — the transaction is not the safety net here,
        # refusing is.
        blocking = []
        for draft in drafts:
            block = blocks.get(draft.object_id)
            if block is None:
                continue
            for check in _checklist_for_block(block, draft.payload):
                if check["level"] == "block":
                    blocking.append({**check, "section": block.section})
        if blocking:
            return Response(
                {
                    "detail": (
                        "This can’t go live yet — "
                        + blocking[0]["label"].lower() + "."
                    ),
                    "blocking": blocking,
                },
                status=status.HTTP_409_CONFLICT,
            )

        allowed = _editable_fields(page["model"])
        published = []
        for draft in drafts:
            block = blocks.get(draft.object_id)
            if block is None:
                draft.delete()
                continue

            before = snapshot_of(block)
            payload = {
                field: value for field, value in (draft.payload or {}).items()
                if field in allowed
            }

            invalid = _field_errors(block, payload)
            if invalid:
                first = sorted(invalid)[0]
                return Response(
                    {
                        "detail": (
                            "This can’t go live yet — "
                            f"“{block.get_section_display()}” has a value that "
                            f"won’t fit ({first}). Fix it and publish again."
                        ),
                        "invalid": {block.section: invalid},
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            applied = []
            for field, value in payload.items():
                setattr(block, field, value)
                applied.append(field)

            if not applied:
                draft.delete()
                continue

            # A drafted `status` is the editor's explicit show/hide choice, so
            # it wins. Only default to PUBLISHED when they didn't say one way or
            # the other: the checklist promises that publishing a hidden section
            # "still won't show", and force-setting PUBLISHED here broke that
            # promise — a typo fix on a deliberately-archived section silently
            # pushed it onto the live homepage.
            if "status" in applied:
                pass  # the editor said explicitly; their choice wins
            elif block.status != PublishStatus.ARCHIVED:
                block.status = PublishStatus.PUBLISHED
                applied.append("status")

            # Only the columns this publish actually touched. A full-row UPDATE
            # writes back every field from a snapshot read before the
            # transaction started, reverting a concurrent author's edit to a
            # field this draft never mentioned — which is exactly what the
            # class docstring above promises cannot happen.
            written = set(applied)
            if any(f.name == "updated_at" for f in block._meta.fields):
                written.add("updated_at")
            block.save(update_fields=sorted(written))
            record_revision(
                block,
                ContentRevision.ACTION_PUBLISHED,
                actor=request.user,
                note=f"Published {len(applied)} change{'' if len(applied) == 1 else 's'}",
                snapshot=before,
            )
            published.append({"section": block.section, "fields": sorted(applied)})
            draft.delete()

        return Response({
            "detail": "Published.",
            "published": published,
            "section_count": len(published),
        })
