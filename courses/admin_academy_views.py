"""Admin academy endpoints: teacher assignment and batch management.

These back the admin panel's Course Management screen so teacher assignment and
batches no longer require Django admin. They mirror the style of the existing
Admin*View classes in courses/views.py (APIView + [IsAuthenticated, IsAdmin]).

Teacher assignment is backed entirely by TeachingAssignment (batch=NULL means
course-wide — the same convention every other content model here uses). The
legacy course-wide-only SubjectTeacher model has been retired; its rows were
migrated in as batch=NULL TeachingAssignments.

Routes (added in courses/urls.py):

    GET    courses/admin/teachers/?q=            approved teachers (assign picker)

    GET    courses/admin/subjects/<subject_id>/teachers/     list course-wide assignments
    POST   courses/admin/subjects/<subject_id>/teachers/     assign a teacher, course-wide
           body: { "teacher_id": "<uuid>", "display_role": "PRIMARY"|"ASSISTANT" }
    PATCH  courses/admin/subject-teachers/<uuid:assignment_id>/   change role
    DELETE courses/admin/subject-teachers/<uuid:assignment_id>/   unassign (soft end)

    GET    courses/admin/courses/<course_id>/batches/       list batches
    POST   courses/admin/courses/<course_id>/batches/       create batch
    PATCH  courses/admin/batches/<batch_id>/                update batch
    DELETE courses/admin/batches/<batch_id>/                delete batch

    GET/POST courses/admin/batches/<batch_id>/teaching-assignments/   per-batch roster
    PATCH/DELETE courses/admin/teaching-assignments/<uuid:assignment_id>/

    GET  courses/admin/courses/<course_id>/staffing/                whole-course grid
    POST courses/admin/courses/<course_id>/staffing/bulk-assign/    one teacher -> many subjects
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import TeacherProfile
from accounts.permissions import IsAdmin
from .models import Batch, Course, Subject, TeachingAssignment

User = get_user_model()

# The course-wide (batch=NULL) assign endpoints only ever offer these two —
# SUBSTITUTE is a batch-roster concept (a temporary stand-in for a specific
# batch's classes), not something that makes sense course-wide.
VALID_ROLES = (TeachingAssignment.ROLE_PRIMARY, TeachingAssignment.ROLE_ASSISTANT)
VALID_TA_ROLES = (
    TeachingAssignment.ROLE_PRIMARY,
    TeachingAssignment.ROLE_ASSISTANT,
    TeachingAssignment.ROLE_SUBSTITUTE,
)


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #
def _teacher_name(user):
    if user is None:
        # TeachingAssignment.teacher is SET_NULL — a hard-deleted teacher
        # account leaves the audit-trail row in place with no user to name.
        return "(deleted teacher)"
    lp = None
    if hasattr(user, "default_learner_profile"):
        try:
            lp = user.default_learner_profile()
        except Exception:
            lp = None
    if lp and getattr(lp, "full_name", ""):
        return lp.full_name
    full = (user.get_full_name() or "").strip()
    return full or user.username or user.email


def is_academy_faculty(tp):
    """Null-safe wrapper over TeacherProfile.is_academy_faculty.

    The rule itself now lives on the model, so `accounts` (the teacher
    directory) and `courses` (subject assignment) share one definition
    instead of restating it — see that property for why it is neither
    `is_approved` nor `teacher_type`. Kept as a function because callers here
    pass a possibly-None profile.
    """
    return bool(tp and tp.is_academy_faculty)


def academy_faculty_users():
    """Base queryset for 'who may teach an Academy subject'. One definition so
    the assign picker and the assign write-path can never drift apart."""
    return (
        User.objects.filter(
            teacher_profile__academy_status=TeacherProfile.TRACK_APPROVED,
            user_roles__role__name="TEACHER",
            user_roles__is_active=True,
        )
        .select_related("teacher_profile")
        .distinct()
    )


def _profile_tracks(profile):
    """Approved tracks, computed with no extra queries. AdminTeacherDirectoryView
    builds a richer version (it also counts an ExpertProfile row) — this is the
    cheap one for the lean picker."""
    tracks = []
    if profile and getattr(profile, "academy_status", None) == TeacherProfile.TRACK_APPROVED:
        tracks.append("academy")
    if profile and getattr(profile, "skill_status", None) == TeacherProfile.TRACK_APPROVED:
        tracks.append("skill")
    return tracks


def teacher_brief(user, st=None, request=None):
    """A teacher's assignable/assigned summary, including profile bits the admin
    wants to see at a glance (role, qualification, photo, rating)."""
    if user is None:
        # TeachingAssignment.teacher is SET_NULL — a hard-deleted teacher
        # account leaves this row's brief with nothing to describe.
        data = {
            "user_id": None,
            "name": _teacher_name(None),
            "email": "",
            "qualification": "",
            "rating": None,
            "photo": None,
            "tracks": [],
        }
        if st is not None:
            data["assignment_id"] = str(st.id)
            data["display_role"] = st.role
            data["order"] = st.order
        return data

    profile = getattr(user, "teacher_profile", None)

    photo = None
    if profile and getattr(profile, "photo", None):
        try:
            photo = request.build_absolute_uri(profile.photo.url) if request else profile.photo.url
        except Exception:
            photo = None

    data = {
        "user_id": str(user.id),
        "name": _teacher_name(user),
        "email": user.email,
        "qualification": (getattr(profile, "qualification", "") or ""),
        "rating": float(profile.rating) if (profile and profile.rating is not None) else None,
        "photo": photo,
        # So the admin can tell an Academy-only teacher from one who also sells
        # skill sessions, instead of guessing from the name.
        "tracks": _profile_tracks(profile),
    }
    if st is not None:
        data["assignment_id"] = str(st.id)  # TeachingAssignment pk (UUID)
        data["display_role"] = st.role
        data["order"] = st.order
    return data


def subject_teachers_payload(subject, request=None):
    """Ordered list of a subject's course-wide (batch=NULL) assigned teachers.
    Callers should prefetch ``teaching_assignments`` to avoid a query per
    subject."""
    tas = (
        subject.teaching_assignments
        .filter(batch__isnull=True, is_active=True)
        .select_related("teacher", "teacher__teacher_profile")
        .order_by("order")
    )
    return [teacher_brief(ta.teacher, ta, request) for ta in tas]


def _batch_payload(b):
    # Prefer the annotated seat count (one query for the whole list); fall back
    # to the model property for single-object responses.
    seats = getattr(b, "_seats", None)
    if seats is None:
        seats = b.seats_taken
    return {
        "id": str(b.id),
        "name": b.name,
        "code": b.code,
        "year": b.year,
        "start_date": b.start_date.isoformat() if b.start_date else None,
        "end_date": b.end_date.isoformat() if b.end_date else None,
        "capacity": b.capacity,
        "price_override": b.price_override,
        "effective_price": b.effective_price,
        "is_active": b.is_active,
        "seats_taken": seats,
        "is_full": (b.capacity is not None and seats >= b.capacity),
    }


def _parse_int_or_none(value):
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date_or_none(value):
    if value in (None, "", "null"):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _apply_optional_batch_fields(batch, data):
    if "year" in data:
        batch.year = _parse_int_or_none(data.get("year"))
    if "capacity" in data:
        batch.capacity = _parse_int_or_none(data.get("capacity"))
    if "price_override" in data:
        batch.price_override = _parse_int_or_none(data.get("price_override"))
    if "start_date" in data:
        batch.start_date = _parse_date_or_none(data.get("start_date"))
    if "end_date" in data:
        batch.end_date = _parse_date_or_none(data.get("end_date"))


# --------------------------------------------------------------------------- #
# Teacher picker
# --------------------------------------------------------------------------- #
class AdminTeacherListView(APIView):
    """Academy faculty available to assign to a subject. Optional ?q= search.

    Returns {"data": [...], "count": <total matching>, "has_more": bool} rather
    than a bare truncated list: the previous version hard-sliced to 100 with no
    count, so a teacher ranked 101st was simply invisible and the UI had no way
    to know it was only showing part of the answer.
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    PAGE_SIZE = 50

    def get(self, request):
        q = request.query_params.get("q", "").strip()
        # Academy-approved only — a self-registered guest expert must not be
        # offered as a school subject teacher. See is_academy_faculty().
        qs = academy_faculty_users()
        if q:
            qs = qs.filter(
                Q(email__icontains=q)
                | Q(username__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
            )
        # Stable ordering — without it the slice below returns an arbitrary
        # subset that can change between identical requests.
        qs = qs.order_by("first_name", "last_name", "email")

        total = qs.count()
        rows = [teacher_brief(u, request=request) for u in qs[: self.PAGE_SIZE]]
        return Response({
            "data": rows,
            "count": total,
            "has_more": total > len(rows),
        })


# --------------------------------------------------------------------------- #
# Teachers directory (rich) + detail — backs the admin "Teachers" screen
# --------------------------------------------------------------------------- #
def _class_range(levels):
    lv = sorted({int(x) for x in levels if x is not None})
    if not lv:
        return None
    return f"{lv[0]}" if len(lv) == 1 else f"{lv[0]}–{lv[-1]}"


def _weekly_hours_map(teacher_ids):
    """teacher_id -> hours taught in the last 7 days (one grouped query)."""
    from datetime import timedelta

    from django.db.models import DurationField, ExpressionWrapper, F, Sum

    from livestream.models import LiveSession

    since = timezone.now() - timedelta(days=7)
    rows = (
        LiveSession.objects.filter(created_by_id__in=teacher_ids, start_time__gte=since)
        .exclude(status=LiveSession.STATUS_CANCELLED)
        .values("created_by_id")
        .annotate(total=Sum(ExpressionWrapper(F("end_time") - F("start_time"), output_field=DurationField())))
    )
    out = {}
    for r in rows:
        total = r["total"]
        out[r["created_by_id"]] = round(total.total_seconds() / 3600, 1) if total else 0.0
    return out


def _assignments_map(teacher_ids):
    """teacher_id -> {subjects:set, levels:set, courses:{course_id: group}}.

    Grouped by COURSE, not flattened to subject names, because a subject name
    on its own does not identify anything an admin can act on. Prod carries
    "Class 10" under BOTH CBSE and MBSE (and the same for Class 8, 9, 11 Arts,
    11 Commerce, 11 Science, 12 Arts, 12 Commerce, 12 Science), so a card
    reading "Economics, Economics (Macroeconomics)" is genuinely ambiguous —
    it could be either board, and the MBSE courses carry no CourseCategory at
    all, so category cannot be the discriminator either. The board, the class
    level and the course title together can be.

    The batch/course-wide split is folded into the subject rather than left as
    two rows: 171 of 172 staffed subjects carry BOTH a course-wide and a
    batch-scoped assignment, so listing rows verbatim shows every subject
    twice.
    """
    tas = (
        TeachingAssignment.objects.filter(teacher_id__in=teacher_ids, is_active=True)
        .select_related(
            "subject", "subject__course", "subject__course__board",
            "subject__course__stream", "batch",
        )
        .prefetch_related("subject__course__categories")
    )
    out = {}
    for ta in tas:
        if not ta.subject_id:
            continue
        subject = ta.subject
        course = subject.course
        d = out.setdefault(
            ta.teacher_id, {"subjects": set(), "levels": set(), "courses": {}},
        )
        d["subjects"].add(subject.name)
        lvl = getattr(course, "class_level", None)
        if lvl is not None:
            d["levels"].add(lvl)

        group = d["courses"].get(course.id)
        if group is None:
            group = d["courses"][course.id] = {
                "course_id": str(course.id),
                "course_title": course.title,
                "board": course.board.name if course.board_id else None,
                "board_slug": course.board.slug if course.board_id else None,
                "class_level": lvl,
                "stream": course.stream.name if course.stream_id else None,
                "kind": course.kind,
                "status": course.status,
                "categories": [c.name for c in course.categories.all()],
                "subjects": {},
            }
        row = group["subjects"].setdefault(subject.id, {
            "subject_id": str(subject.id),
            "name": subject.name,
            "roles": set(),
            "batches": set(),
            "course_wide": False,
        })
        row["roles"].add(ta.role)
        if ta.batch_id:
            row["batches"].add(ta.batch.code or ta.batch.name)
        else:
            row["course_wide"] = True
    return out


def _course_groups(asg):
    """The per-course groups from _assignments_map, as a sorted JSON-safe list.

    Ordered the way an admin scans them: school courses by class level first
    (lowest to highest), then coaching courses with no class level, and boards
    alphabetically within a level so the CBSE/MBSE twins sit next to each other.
    """
    groups = []
    for group in asg.get("courses", {}).values():
        subjects = sorted(
            (
                {
                    "subject_id": s["subject_id"],
                    "name": s["name"],
                    "roles": sorted(s["roles"]),
                    "batches": sorted(s["batches"]),
                    "course_wide": s["course_wide"],
                }
                for s in group["subjects"].values()
            ),
            key=lambda s: s["name"].lower(),
        )
        groups.append({**group, "subjects": subjects, "subject_count": len(subjects)})
    groups.sort(key=lambda g: (
        g["class_level"] is None,          # coaching (no class level) last
        g["class_level"] or 0,
        (g["board"] or "").lower(),
        g["course_title"].lower(),
    ))
    return groups


def _skill_map(teacher_ids):
    """teacher_id -> {sessions_count, earnings(paise), categories:[label]} or absent."""
    from django.db.models import Sum

    from skills.models import ExpertProfile, SkillSession

    experts = (
        ExpertProfile.objects.filter(teacher_profile__user_id__in=teacher_ids)
        .select_related("teacher_profile")
        .prefetch_related("categories")
    )
    out = {}
    for ep in experts:
        out[ep.teacher_profile.user_id] = {
            "sessions_count": ep.sessions_count,
            "categories": [c.label for c in ep.categories.all()],
            "earnings": 0,
            "rating": float(ep.rating) if ep.rating is not None else None,
        }
    if out:
        earn = (
            SkillSession.objects.filter(
                expert__teacher_profile__user_id__in=teacher_ids,
                status=SkillSession.STATUS_COMPLETED,
            )
            .values("expert__teacher_profile__user_id")
            .annotate(total=Sum("amount"))
        )
        for r in earn:
            uid = r["expert__teacher_profile__user_id"]
            if uid in out:
                out[uid]["earnings"] = r["total"] or 0
    return out


def _teacher_row(user, request, hours_map, asg_map, skill_map):
    data = teacher_brief(user, request=request)
    profile = getattr(user, "teacher_profile", None)
    tracks = []
    if profile and getattr(profile, "academy_status", None) == "approved":
        tracks.append("academy")
    if user.id in skill_map or (profile and getattr(profile, "skill_status", None) == "approved"):
        tracks.append("skill")
    asg = asg_map.get(user.id, {"subjects": set(), "levels": set(), "courses": {}})
    groups = _course_groups(asg)
    data.update({
        "tracks": tracks or ["academy"],
        "subjects": sorted(asg["subjects"]),
        # What the teacher actually covers, grouped by course so board / class
        # level / category travel with every subject name.
        "courses": groups,
        "course_count": len(groups),
        "subject_count": sum(g["subject_count"] for g in groups),
        "boards": sorted({g["board"] for g in groups if g["board"]}),
        "class_range": _class_range(asg["levels"]),
        "weekly_hours": hours_map.get(user.id, 0.0),
        "since": user.date_joined.isoformat() if user.date_joined else None,
        "skill": skill_map.get(user.id),
    })
    return data


class AdminTeacherDirectoryView(APIView):
    """Rich teacher directory for the admin Teachers screen.

    GET /courses/admin/teacher-directory/?q=&track=academy|skill
                                        &board=<slug>&class_level=<int>
    Distinct from the lean AdminTeacherListView (the assign picker) so that
    screen's contract stays stable.

    ``board`` and ``class_level`` filter on what the teacher actively teaches,
    which is the only way to separate the CBSE "Class 10" faculty from the
    MBSE "Class 10" faculty — the two courses share a title.
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def _filter_options(self, base_qs):
        """Boards and class levels that at least one listed teacher covers.

        Built from the assignments of the base (unfiltered-by-facet) queryset
        so a chip never offers a combination that returns nothing.
        """
        rows = (
            TeachingAssignment.objects.filter(
                is_active=True, teacher__in=base_qs,
            )
            .values(
                "subject__course__board__slug",
                "subject__course__board__name",
                "subject__course__class_level",
            )
            .distinct()
        )
        boards, levels = {}, set()
        for r in rows:
            slug = r["subject__course__board__slug"]
            if slug:
                boards[slug] = r["subject__course__board__name"]
            if r["subject__course__class_level"] is not None:
                levels.add(r["subject__course__class_level"])
        return {
            "boards": [
                {"slug": s, "name": n} for s, n in sorted(boards.items(), key=lambda kv: kv[1])
            ],
            "class_levels": sorted(levels),
        }

    def get(self, request):
        q = request.query_params.get("q", "").strip()
        track = request.query_params.get("track", "").strip().lower()
        board = request.query_params.get("board", "").strip()
        class_level = request.query_params.get("class_level", "").strip()
        qs = (
            User.objects.filter(
                teacher_profile__is_approved=True,
                user_roles__role__name="TEACHER",
                user_roles__is_active=True,
            )
            .select_related("teacher_profile")
            .distinct()
        )
        if q:
            qs = qs.filter(
                Q(email__icontains=q) | Q(username__icontains=q)
                | Q(first_name__icontains=q) | Q(last_name__icontains=q)
            )
        # Filter by track in the DATABASE, before the slice. This used to run in
        # Python on the already-truncated 200 rows, so ?track=academy returned an
        # arbitrary subset once there were more than 200 approved teachers.
        # Mirrors _teacher_row's definition: skill = an ExpertProfile row exists
        # OR the skill track is approved.
        if track == "academy":
            qs = qs.filter(teacher_profile__academy_status=TeacherProfile.TRACK_APPROVED)
        elif track == "skill":
            qs = qs.filter(
                Q(teacher_profile__expert_profile__isnull=False)
                | Q(teacher_profile__skill_status=TeacherProfile.TRACK_APPROVED)
            )
        # Options are computed BEFORE the board/class facets are applied, so
        # picking "CBSE" doesn't wipe every other chip off the screen.
        options = self._filter_options(qs)

        # ONE filter() call, not two. Two chained filters on a multi-valued
        # relation produce two independent joins, so board=cbse&class_level=10
        # would match a teacher who covers something CBSE and, separately,
        # something in class 10 — not the CBSE Class 10 course they were
        # actually asked about.
        facets = {}
        if board:
            facets["teaching_assignments__subject__course__board__slug"] = board
        if class_level.isdigit():
            facets["teaching_assignments__subject__course__class_level"] = int(class_level)
        if facets:
            qs = qs.filter(teaching_assignments__is_active=True, **facets)

        qs = qs.order_by("first_name", "last_name", "email").distinct()

        total = qs.count()
        users = list(qs[:200])
        ids = [u.id for u in users]
        hours_map = _weekly_hours_map(ids)
        asg_map = _assignments_map(ids)
        skill_map = _skill_map(ids)
        rows = [_teacher_row(u, request, hours_map, asg_map, skill_map) for u in users]
        return Response({
            "data": rows,
            "count": total,
            "has_more": total > len(rows),
            "filters": options,
        })


class AdminTeacherDetailView(APIView):
    """GET /courses/admin/teachers/<uuid:user_id>/ — one teacher + assignments +
    recent activity."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, user_id):
        user = get_object_or_404(
            User.objects.select_related("teacher_profile"), id=user_id
        )
        hours_map = _weekly_hours_map([user.id])
        asg_map = _assignments_map([user.id])
        skill_map = _skill_map([user.id])
        data = _teacher_row(user, request, hours_map, asg_map, skill_map)

        # Flat assignment roster, kept for anything reading the old shape —
        # but derived from data["courses"] rather than re-queried, so it is
        # one row per SUBJECT (course-wide and batch coverage merged) and each
        # row now says which course, board and class the subject belongs to.
        data["assignments"] = [
            {
                "subject": s["name"],
                "course_title": g["course_title"],
                "board": g["board"],
                "class_level": g["class_level"],
                "batches": s["batches"],
                "course_wide": s["course_wide"],
                "batch_code": s["batches"][0] if s["batches"] else None,
                "role": s["roles"][0] if s["roles"] else None,
            }
            for g in data["courses"]
            for s in g["subjects"]
        ]

        # Recent activity — last few live classes taught
        from livestream.models import LiveSession

        recent = (
            LiveSession.objects.filter(created_by=user)
            .select_related("subject")
            .order_by("-start_time")[:10]
        )
        data["recent_activity"] = [
            {
                "type": "live",
                "text": f"{s.title} · {s.subject.name if s.subject_id else ''}",
                "when": (s.actual_started_at or s.start_time).isoformat() if (s.actual_started_at or s.start_time) else None,
                "status": s.computed_status(),
            }
            for s in recent
        ]
        return Response(data)


# --------------------------------------------------------------------------- #
# Subject <-> teacher assignment
# --------------------------------------------------------------------------- #
class AdminSubjectTeachersView(APIView):
    """Course-wide (batch=NULL) assignment — the "Teachers" button on a
    subject row, independent of any specific batch."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, subject_id):
        subject = get_object_or_404(Subject, id=subject_id)
        return Response(subject_teachers_payload(subject, request))

    def post(self, request, subject_id):
        subject = get_object_or_404(Subject, id=subject_id)

        teacher_id = request.data.get("teacher_id") or request.data.get("user_id")
        if not teacher_id:
            return Response(
                {"detail": "teacher_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        role = (request.data.get("display_role") or TeachingAssignment.ROLE_PRIMARY).upper()
        if role not in VALID_ROLES:
            role = TeachingAssignment.ROLE_PRIMARY

        try:
            teacher = User.objects.select_related("teacher_profile").get(pk=teacher_id)
        except (User.DoesNotExist, DjangoValidationError, ValueError):
            return Response(
                {"detail": "Teacher not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        tp = getattr(teacher, "teacher_profile", None)
        if not is_academy_faculty(tp):
            return Response(
                {"detail": "Only approved Academy faculty can be assigned to a subject."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        next_order = (
            TeachingAssignment.objects.filter(subject=subject, batch__isnull=True)
            .order_by("-order")
            .values_list("order", flat=True)
            .first()
        )
        order = (next_order or 0) + 1

        try:
            ta = TeachingAssignment.objects.create(
                subject=subject, batch=None, teacher=teacher, role=role,
                order=order, is_active=True, assigned_by=request.user,
            )
        except IntegrityError:
            return Response(
                {"detail": "This teacher is already assigned to this subject."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(teacher_brief(teacher, ta, request), status=status.HTTP_201_CREATED)


class AdminSubjectTeacherDetailView(APIView):
    """PATCH the role, or DELETE (soft end) a course-wide assignment. Shares
    the underlying model with the batch roster below (courses/urls.py's
    AdminTeachingAssignmentDetailView) — kept as a separate URL because the
    "Teachers" modal on a subject row only ever deals with batch=NULL rows."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, assignment_id):
        ta = get_object_or_404(
            TeachingAssignment.objects.select_related("teacher", "teacher__teacher_profile"),
            pk=assignment_id, batch__isnull=True,
        )
        role = (request.data.get("display_role") or "").upper()
        if role in VALID_ROLES and role != ta.role:
            ta.role = role
            try:
                ta.save(update_fields=["role"])
            except IntegrityError:
                return Response(
                    {"detail": "This subject already has an active primary teacher."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return Response(teacher_brief(ta.teacher, ta, request))

    def delete(self, request, assignment_id):
        ta = get_object_or_404(TeachingAssignment, pk=assignment_id, batch__isnull=True)
        ta.is_active = False
        ta.ended_at = timezone.now()
        ta.save(update_fields=["is_active", "ended_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Course-wide staffing — the whole subjects x teachers grid in one call
# --------------------------------------------------------------------------- #
class AdminCourseStaffingView(APIView):
    """GET /courses/admin/courses/<uuid:course_id>/staffing/

    Every subject in the course with its assigned teachers, in ONE request.
    The admin Courses screen previously fetched a subject's teachers per row
    (one request per subject); at 116 subjects in production that is 116
    round-trips to render one table, re-fired on every assignment change.
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, course_id):
        course = get_object_or_404(Course, id=course_id)
        subjects = (
            course.subjects
            .prefetch_related(
                "teaching_assignments__teacher__teacher_profile",
            )
            .order_by("order", "name")
        )
        rows = [
            {
                "id": str(s.id),
                "name": s.name,
                "order": s.order,
                "teachers": subject_teachers_payload(s, request),
            }
            for s in subjects
        ]
        return Response({
            "course": {"id": str(course.id), "title": course.title},
            "subjects": rows,
            "unstaffed_count": sum(1 for r in rows if not r["teachers"]),
        })


class AdminCourseBulkAssignView(APIView):
    """POST /courses/admin/courses/<uuid:course_id>/staffing/bulk-assign/

    Assign ONE teacher to MANY subjects of a course at once —
    {teacher_id, subject_ids: [...], display_role}.

    Idempotent: a subject the teacher already holds is reported in `skipped`
    rather than failing the whole call, so re-running after a partial failure
    is safe. All-or-nothing within the request.
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, course_id):
        course = get_object_or_404(Course, id=course_id)

        teacher_id = request.data.get("teacher_id") or request.data.get("user_id")
        subject_ids = request.data.get("subject_ids") or []
        if not teacher_id:
            return Response({"detail": "teacher_id is required."},
                            status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(subject_ids, list) or not subject_ids:
            return Response({"detail": "subject_ids must be a non-empty list."},
                            status=status.HTTP_400_BAD_REQUEST)

        role = (request.data.get("display_role") or TeachingAssignment.ROLE_PRIMARY).upper()
        if role not in VALID_ROLES:
            role = TeachingAssignment.ROLE_PRIMARY

        try:
            teacher = User.objects.select_related("teacher_profile").get(pk=teacher_id)
        except (User.DoesNotExist, DjangoValidationError, ValueError):
            return Response({"detail": "Teacher not found."},
                            status=status.HTTP_404_NOT_FOUND)

        # Same gate as the single-subject assign — an auto-approved Skill-track
        # guest expert must not become an Academy subject teacher in bulk either.
        if not is_academy_faculty(getattr(teacher, "teacher_profile", None)):
            return Response(
                {"detail": "Only approved Academy faculty can be assigned to a subject."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Only subjects that really belong to this course — a stale/forged id
        # must not silently staff someone onto another course's subject.
        valid = {
            str(s.id): s
            for s in Subject.objects.filter(course=course, id__in=subject_ids)
        }
        unknown = [str(i) for i in subject_ids if str(i) not in valid]

        already = set(
            TeachingAssignment.objects
            .filter(subject_id__in=valid.keys(), teacher=teacher,
                    batch__isnull=True, is_active=True)
            .values_list("subject_id", flat=True)
        )

        # One query for the current max order per subject, instead of one per
        # subject inside the loop.
        max_orders = {
            str(r["subject_id"]): r["mx"]
            for r in TeachingAssignment.objects
            .filter(subject_id__in=valid.keys(), batch__isnull=True)
            .values("subject_id")
            .annotate(mx=Max("order"))
        }

        created = []
        with transaction.atomic():
            for sid, subject in valid.items():
                if subject.id in already:
                    continue
                created.append(TeachingAssignment(
                    subject=subject,
                    batch=None,
                    teacher=teacher,
                    role=role,
                    order=(max_orders.get(sid) or 0) + 1,
                    is_active=True,
                    assigned_by=request.user,
                ))
            if created:
                TeachingAssignment.objects.bulk_create(created)

        return Response(
            {
                "assigned": len(created),
                "skipped_already_assigned": [
                    str(i) for i in already
                ],
                "skipped_not_in_course": unknown,
                "teacher": teacher_brief(teacher, request=request),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


# --------------------------------------------------------------------------- #
# Batch management
# --------------------------------------------------------------------------- #
class AdminCourseBatchesView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, course_id):
        get_object_or_404(Course, id=course_id)
        batches = (
            Batch.objects.filter(course_id=course_id)
            .annotate(_seats=Count("enrollments", filter=Q(enrollments__status="ACTIVE")))
            .order_by("-year", "code")
        )
        return Response([_batch_payload(b) for b in batches])

    def post(self, request, course_id):
        course = get_object_or_404(Course, id=course_id)

        name = (request.data.get("name") or "").strip()
        code = (request.data.get("code") or "").strip()
        if not name or not code:
            return Response(
                {"detail": "Batch name and code are both required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch = Batch(
            course=course,
            name=name,
            code=code,
            is_active=bool(request.data.get("is_active", True)),
        )
        _apply_optional_batch_fields(batch, request.data)

        try:
            batch.save()
        except IntegrityError:
            return Response(
                {"detail": f"A batch with code '{code}' already exists in this course."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(_batch_payload(batch), status=status.HTTP_201_CREATED)


class AdminBatchDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, batch_id):
        batch = get_object_or_404(Batch, id=batch_id)

        if "name" in request.data and request.data.get("name") is not None:
            batch.name = str(request.data["name"]).strip() or batch.name
        if "code" in request.data and request.data.get("code") is not None:
            batch.code = str(request.data["code"]).strip() or batch.code
        if "is_active" in request.data:
            batch.is_active = bool(request.data["is_active"])
        _apply_optional_batch_fields(batch, request.data)

        try:
            batch.save()
        except IntegrityError:
            return Response(
                {"detail": f"A batch with code '{batch.code}' already exists in this course."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(_batch_payload(batch))

    def delete(self, request, batch_id):
        batch = get_object_or_404(Batch, id=batch_id)
        # Enrollment.batch is SET_NULL, so deleting a batch detaches its students
        # rather than deleting them. Progress rows (CASCADE) for this batch go too.
        batch.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Staffing matrix — per-batch teaching assignments (TeachingAssignment)
# --------------------------------------------------------------------------- #
def _teaching_assignment_payload(ta, request=None):
    return {
        "assignment_id": str(ta.id),
        "batch_id": str(ta.batch_id),
        "subject_id": str(ta.subject_id),
        "role": ta.role,
        "order": ta.order,
        "teacher": teacher_brief(ta.teacher, request=request),
    }


class AdminBatchTeachingAssignmentsView(APIView):
    """Staffing matrix for one batch: who teaches which subject.

    GET  -> active TeachingAssignment rows for the batch (with teacher briefs).
    POST -> assign a teacher to a subject in this batch.
            body: { "subject_id": <uuid>, "teacher_id": <uuid>,
                    "role": "PRIMARY"|"ASSISTANT"|"SUBSTITUTE" }
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, batch_id):
        get_object_or_404(Batch, id=batch_id)
        rows = (
            TeachingAssignment.objects
            .filter(batch_id=batch_id, is_active=True)
            .select_related("teacher", "teacher__teacher_profile", "subject")
            .order_by("subject__order", "order")
        )
        return Response([_teaching_assignment_payload(ta, request) for ta in rows])

    def post(self, request, batch_id):
        batch = get_object_or_404(Batch.objects.select_related("course"), id=batch_id)

        subject_id = request.data.get("subject_id")
        teacher_id = request.data.get("teacher_id") or request.data.get("user_id")
        if not subject_id or not teacher_id:
            return Response(
                {"detail": "subject_id and teacher_id are both required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        role = (request.data.get("role") or TeachingAssignment.ROLE_PRIMARY).upper()
        if role not in VALID_TA_ROLES:
            role = TeachingAssignment.ROLE_PRIMARY

        subject = get_object_or_404(Subject, id=subject_id)
        # Triangle guard: the subject must belong to this batch's course.
        if subject.course_id != batch.course_id:
            return Response(
                {"detail": "Subject and batch belong to different courses."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            teacher = User.objects.select_related("teacher_profile").get(pk=teacher_id)
        except (User.DoesNotExist, DjangoValidationError, ValueError):
            return Response(
                {"detail": "Teacher not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        tp = getattr(teacher, "teacher_profile", None)
        if not is_academy_faculty(tp):
            return Response(
                {"detail": "Only approved Academy faculty can be assigned to a subject."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        next_order = (
            TeachingAssignment.objects
            .filter(batch=batch, subject=subject, is_active=True)
            .order_by("-order")
            .values_list("order", flat=True)
            .first()
        )
        order = (next_order or 0) + 1

        try:
            ta = TeachingAssignment.objects.create(
                batch=batch, subject=subject, teacher=teacher,
                role=role, order=order, is_active=True,
                assigned_by=request.user,
            )
        except IntegrityError:
            # Violates one of the partial-unique constraints: either this
            # teacher is already active here, or a PRIMARY already exists.
            if role == TeachingAssignment.ROLE_PRIMARY:
                msg = ("This subject already has an active primary teacher in "
                       "this batch. End it first or assign as assistant.")
            else:
                msg = "This teacher is already assigned to this subject in this batch."
            return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            _teaching_assignment_payload(ta, request),
            status=status.HTTP_201_CREATED,
        )


class AdminTeachingAssignmentDetailView(APIView):
    """PATCH the role, or DELETE (end) a teaching assignment.

    DELETE is a soft end (is_active=False, ended_at=now) so the roster history
    "who taught this batch's maths in July" is preserved. This is now the
    complete revocation act — TeachingAssignment is the only model granting
    subject access (services.teaches_subject() reads only this table), so
    there is no second row anywhere that could keep a removed teacher's
    access alive. (Prior to SubjectTeacher's retirement, that mirrored legacy
    row had to be cleared here too — see git history if you need the story.)
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def patch(self, request, assignment_id):
        ta = get_object_or_404(
            TeachingAssignment.objects.select_related(
                "teacher", "teacher__teacher_profile"),
            pk=assignment_id,
        )
        role = (request.data.get("role") or "").upper()
        if role in VALID_TA_ROLES and role != ta.role:
            ta.role = role
            try:
                ta.save(update_fields=["role"])
            except IntegrityError:
                return Response(
                    {"detail": "This subject already has an active primary "
                               "teacher in this batch."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return Response(_teaching_assignment_payload(ta, request))

    def delete(self, request, assignment_id):
        ta = get_object_or_404(TeachingAssignment, pk=assignment_id)
        ta.is_active = False
        ta.ended_at = timezone.now()
        ta.save(update_fields=["is_active", "ended_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
