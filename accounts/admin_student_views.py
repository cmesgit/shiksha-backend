"""Admin student management.

A "student" in this app is a ``LearnerProfile``, NOT a ``User``: one account
can hold several learner profiles (a parent with three children is 1 user and
3 students). The existing ``admin/users/`` endpoints are therefore an
*account* directory and cannot answer per-student questions — these endpoints
are the student directory.

    GET   admin/students/                      paginated/searchable/filterable list
    GET   admin/students/<profile_id>/         one student + academic detail
    PATCH admin/students/<profile_id>/         edit their details
    POST  admin/students/<profile_id>/active/  {is_active} deactivate/reactivate

⚠️ Per-student vs account-scoped data — the central caveat of this module.
Enrollment, Subscription, QuizAttempt, AssignmentSubmission, Activity and
PrivateSession are all keyed to a learner profile, so they are genuinely
attributable to ONE student. Video progress and every flavour of session
attendance are keyed to the ACCOUNT only (no learner_profile column exists),
so on a multi-child account they cannot be attributed to an individual child.
Those are returned under a separate ``account_scoped`` block carrying an
explicit ``shared_across_profiles`` count so the UI can label them instead of
implying they belong to the student being viewed. Do not move fields between
these two blocks without checking the underlying model's FK.
"""

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from assignments.models import AssignmentSubmission
from courses.models_progress import VideoProgress
from courses.progress_stats import _dual_key_q, average_quiz_score_pct
from enrollments.models import Enrollment, Subscription
from livestream.models import LiveSessionAttendance
from quizzes.models import QuizAttempt

from .models import LearnerProfile
from .permissions import IsAdmin
from .auth_flow import apply_profile_edits, serialize_profile_detail


# ─────────────────────────── is_complete, in SQL ───────────────────────────
# LearnerProfile.is_complete is a Python @property (accounts/models.py), so it
# cannot be filtered or ordered in the database. This mirrors it as a queryset
# Q so the list endpoint can offer an "incomplete profiles" filter without
# pulling every row into Python (which would also break the paginated count).
#
# ⚠️ These two definitions must change together. If you edit the property, edit
# this; a silent divergence makes the filter lie about which rows are complete.
def _has(field):
    """Django equivalent of ``bool(value)`` for a nullable Char/Text field."""
    return ~Q(**{field: ""}) & ~Q(**{f"{field}__isnull": True})


def complete_profile_q():
    has_personal = (
        _has("first_name")
        & _has("last_name")
        & _has("phone")
        & ~Q(date_of_birth__isnull=True)  # DateField: only NULL is "empty"
    )
    has_address = _has("state") & _has("district") & _has("city_town")
    has_parent_contact = (
        (_has("father_name") & _has("father_phone"))
        | (_has("mother_name") & _has("mother_phone"))
        | (_has("guardian_name") & _has("guardian_phone"))
    )
    has_academic = _has("currently_studying")
    return (
        has_personal
        & has_address
        & has_parent_contact
        & has_academic
        & Q(account__is_verified=True)
    )


def _bool_param(val):
    if val in (None, ""):
        return None
    return str(val).lower() in ("true", "1", "yes")


# ─────────────────────────────── row builders ───────────────────────────────
def _student_row(p, *, sibling_counts=None, enrollment_counts=None, placements=None):
    """One student row. ``sibling_counts``/``enrollment_counts``/``placements``
    are bulk maps (see the list view) so a page of rows costs a constant number
    of queries rather than several per row."""
    if sibling_counts is None:
        sibling_count = p.account.learner_profiles.filter(is_active=True).count() - (
            1 if p.is_active else 0
        )
    else:
        sibling_count = max(0, sibling_counts.get(p.account_id, 0) - (1 if p.is_active else 0))

    if enrollment_counts is None:
        active_enrollments = Enrollment.objects.filter(
            _dual_key_q("user", p.account, p), status=Enrollment.STATUS_ACTIVE
        ).count()
    else:
        active_enrollments = enrollment_counts.get(p.id, 0)

    return {
        # Course→batch placements, so the list can show a Batch column and
        # answer "who still needs a batch?" without opening each student.
        "placements": (placements or {}).get(p.id, []),
        "id": str(p.id),
        "display_name": p.display_name,
        "full_name": p.full_name or f"{p.first_name} {p.last_name}".strip(),
        "student_id": p.student_id or "",
        "relationship": p.relationship,
        "current_class": p.current_class or "",
        "stream": p.stream or "",
        "board": p.board or "",
        "school_name": p.school_name or "",
        "phone": p.phone or "",
        "is_active": p.is_active,
        "is_default": p.is_default,
        "is_complete": p.is_complete,
        "avatar_type": p.avatar_type(),
        "avatar": p.avatar_value(),
        "account": {
            "id": str(p.account_id),
            "email": p.account.email,
            "is_verified": p.account.is_verified,
        },
        "sibling_count": sibling_count,
        "active_enrollment_count": active_enrollments,
        "created_at": p.created_at,
    }


class AdminStudentListView(APIView):
    """Flat directory of learner profiles — one row per student, so siblings
    appear as the separate students they are (sharing an account email)."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = (
            LearnerProfile.objects
            .select_related("account")
            .order_by("-created_at")
        )

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(full_name__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(display_name__icontains=search)
                | Q(student_id__icontains=search)
                | Q(account__email__icontains=search)
            ).distinct()

        for param, field in (
            ("current_class", "current_class"),
            ("stream", "stream"),
            ("board", "board"),
        ):
            val = request.query_params.get(param, "").strip()
            if val:
                qs = qs.filter(**{field: val})

        is_active = _bool_param(request.query_params.get("is_active"))
        if is_active is not None:
            qs = qs.filter(is_active=is_active)

        incomplete = _bool_param(request.query_params.get("incomplete"))
        if incomplete is not None:
            qs = qs.filter(~complete_profile_q() if incomplete else complete_profile_q())

        # ── who counts as a "student" ──
        # Every account gets an auto-created default LearnerProfile at login,
        # and Skill-Dev-only users plus staff never enrol in an academy course —
        # so listing every profile makes the count meaningless. A profile becomes
        # an academy student by ENROLLING, so that's the default view; pass
        # ?enrolled=false or ?enrolled=all to see the rest.
        enrolled_raw = (request.query_params.get("enrolled") or "").strip().lower()
        enrolled = None if enrolled_raw == "all" else _bool_param(enrolled_raw)
        if enrolled_raw == "":
            enrolled = True
        if enrolled is not None:
            # Mirrors _dual_key_q in SQL: an active enrollment on this profile,
            # OR — for the account's DEFAULT profile — a legacy enrollment with
            # no profile link. Getting this wrong would hide real students whose
            # rows predate per-profile enrollment, which is exactly the group an
            # admin most needs to see.
            with_enrollment = Q(enrollments__status=Enrollment.STATUS_ACTIVE) | Q(
                is_default=True,
                account__enrollments__status=Enrollment.STATUS_ACTIVE,
                account__enrollments__learner_profile__isnull=True,
            )
            qs = qs.filter(with_enrollment).distinct() if enrolled else qs.exclude(
                with_enrollment
            ).distinct()

        # ── course / batch placement filters ──
        # These are what an academy admin actually works from: "everyone in
        # A13", and (via ?no_batch=true) "who still needs placing before the
        # session starts".
        course_id = (request.query_params.get("course") or "").strip()
        if course_id:
            qs = qs.filter(
                enrollments__course_id=course_id,
                enrollments__status=Enrollment.STATUS_ACTIVE,
            ).distinct()

        batch_id = (request.query_params.get("batch") or "").strip()
        if batch_id:
            qs = qs.filter(
                enrollments__batch_id=batch_id,
                enrollments__status=Enrollment.STATUS_ACTIVE,
            ).distinct()

        no_batch = _bool_param(request.query_params.get("no_batch"))
        if no_batch is not None:
            # Same dual-key treatment as `enrolled` above — legacy rows are
            # exactly the ones most likely to be missing a batch, so excluding
            # them here would defeat the point of the filter.
            unplaced = Q(
                enrollments__batch__isnull=True,
                enrollments__status=Enrollment.STATUS_ACTIVE,
            ) | Q(
                is_default=True,
                account__enrollments__batch__isnull=True,
                account__enrollments__status=Enrollment.STATUS_ACTIVE,
                account__enrollments__learner_profile__isnull=True,
            )
            qs = qs.filter(unplaced).distinct() if no_batch else qs.exclude(
                unplaced
            ).distinct()

        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = min(100, max(1, int(request.query_params.get("page_size", 25))))
        except (TypeError, ValueError):
            page_size = 25

        count = qs.count()
        start = (page - 1) * page_size
        rows = list(qs[start:start + page_size])

        # Bulk maps — one query each for the whole page instead of per row.
        account_ids = {p.account_id for p in rows}
        sibling_counts = dict(
            LearnerProfile.objects
            .filter(account_id__in=account_ids, is_active=True)
            .values_list("account")
            .annotate(n=Count("id"))
        )
        profile_ids = [p.id for p in rows]
        enrollment_counts = dict(
            Enrollment.objects
            .filter(learner_profile_id__in=profile_ids, status=Enrollment.STATUS_ACTIVE)
            .values_list("learner_profile")
            .annotate(n=Count("id"))
        )
        # Legacy (pre-profile) enrollments have learner_profile=NULL and belong
        # to the account's DEFAULT profile — the same rule _dual_key_q applies.
        # Without this the list's count would disagree with the detail view's
        # enrollment list for any account holding legacy rows.
        legacy_counts = dict(
            Enrollment.objects
            .filter(
                user_id__in=account_ids,
                learner_profile__isnull=True,
                status=Enrollment.STATUS_ACTIVE,
            )
            .values_list("user")
            .annotate(n=Count("id"))
        )
        for p in rows:
            if p.is_default and legacy_counts.get(p.account_id):
                enrollment_counts[p.id] = (
                    enrollment_counts.get(p.id, 0) + legacy_counts[p.account_id]
                )

        # Course→batch placements for the whole page, so the Batch column and the
        # bulk-assign selection have the enrollment ids they need. Two queries:
        # profile-keyed rows, plus legacy NULL-profile rows mapped onto each
        # account's DEFAULT profile — the same dual-key rule used for the counts
        # and the `enrolled` filter. Without the second pass a legacy student
        # reads as "1 course" but "— batches", which looks like a bug to an admin.
        def _placement(e):
            return {
                "enrollment_id": str(e.id),
                "course_id": str(e.course_id),
                "course_title": e.course.title if e.course else "",
                "batch_id": str(e.batch_id) if e.batch_id else None,
                "batch_name": e.batch.name if e.batch else None,
                "batch_code": e.batch.code if e.batch else (e.batch_code or ""),
            }

        placements = {}
        for e in (
            Enrollment.objects
            .filter(learner_profile_id__in=profile_ids, status=Enrollment.STATUS_ACTIVE)
            .select_related("course", "batch")
        ):
            placements.setdefault(e.learner_profile_id, []).append(_placement(e))

        default_profile_by_account = {
            p.account_id: p.id for p in rows if p.is_default
        }
        if default_profile_by_account:
            for e in (
                Enrollment.objects
                .filter(
                    user_id__in=default_profile_by_account.keys(),
                    learner_profile__isnull=True,
                    status=Enrollment.STATUS_ACTIVE,
                )
                .select_related("course", "batch")
            ):
                pid = default_profile_by_account.get(e.user_id)
                if pid:
                    placements.setdefault(pid, []).append(_placement(e))

        return Response({
            "count": count,
            "results": [
                _student_row(
                    p,
                    sibling_counts=sibling_counts,
                    enrollment_counts=enrollment_counts,
                    placements=placements,
                )
                for p in rows
            ],
        })


class AdminStudentDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _get_profile(self, profile_id):
        p = (
            LearnerProfile.objects
            .select_related("account")
            .filter(id=profile_id)
            .first()
        )
        if not p:
            raise ValidationError("Student not found.")
        return p

    def get(self, request, profile_id):
        p = self._get_profile(profile_id)
        account = p.account

        data = _student_row(p)
        # `placements` is a list-view convenience (built from a bulk map); here
        # the full `enrollments` block below is authoritative, so drop the key
        # rather than ship an empty list that contradicts it.
        data.pop("placements", None)
        data["details"] = serialize_profile_detail(p)

        # Siblings — lets the flat list still be navigated as a family unit.
        data["siblings"] = [
            {
                "id": str(s.id),
                "display_name": s.display_name,
                "full_name": s.full_name or "",
                "relationship": s.relationship,
                "current_class": s.current_class or "",
                "is_active": s.is_active,
            }
            for s in account.learner_profiles.exclude(id=p.id).order_by("-is_default", "created_at")
        ]

        # ── per-student (profile-keyed) ──
        subs = {
            s.course_id: s
            for s in Subscription.objects.filter(_dual_key_q("user", account, p))
            .select_related("course")
            .order_by("-expires_at")
        }
        enrollments = (
            Enrollment.objects
            .filter(_dual_key_q("user", account, p))
            .select_related("course", "batch")
            .order_by("-enrolled_at")
        )
        data["enrollments"] = []
        for e in enrollments:
            sub = subs.get(e.course_id)
            data["enrollments"].append({
                "id": str(e.id),
                "course_id": str(e.course_id),
                "course_title": e.course.title if e.course else "",
                "batch_id": str(e.batch_id) if e.batch_id else None,
                "batch_name": e.batch.name if e.batch else None,
                "batch_code": e.batch.code if e.batch else (e.batch_code or ""),
                "status": e.status,
                "enrolled_at": e.enrolled_at,
                "is_legacy_profile_link": e.learner_profile_id is None,
                "subscription": {
                    "status": sub.status,
                    "expires_at": sub.expires_at,
                    # `is_currently_active` is a property on Subscription
                    # (status ACTIVE *and* not past expires_at) — there is no
                    # is_valid() method despite the name reading like one.
                    "is_valid": sub.is_currently_active,
                } if sub else None,
            })

        # order_by started_at, NOT id — QuizAttempt's PK is a UUID, so ordering
        # by it would be arbitrary rather than most-recent-first.
        attempts = list(
            QuizAttempt.objects
            .filter(_dual_key_q("student", account, p))
            .select_related("quiz")
            .order_by("-started_at")[:20]
        )
        data["quiz_attempts"] = [
            {
                "id": str(a.id),
                "quiz_title": a.quiz.title if a.quiz else "",
                "score": a.score,
                "total_marks": a.quiz.total_marks if a.quiz else None,
                "attempt_number": a.attempt_number,
                "status": a.status,
                "submitted_at": a.submitted_at,
            }
            for a in attempts
        ]
        # Average only SUBMITTED attempts — a PENDING (in-progress) attempt has
        # score 0 by default and would drag the average down spuriously.
        data["quiz_avg_pct"] = average_quiz_score_pct(
            [a for a in attempts if a.status == QuizAttempt.STATUS_SUBMITTED]
        )

        # NOTE: AssignmentSubmission carries no marks/grade/feedback field at
        # all — submission is the only fact available, so don't imply grading.
        data["assignment_submissions"] = [
            {
                "id": str(s.id),
                "assignment_title": s.assignment.title if s.assignment else "",
                "submitted_at": s.submitted_at,
            }
            for s in AssignmentSubmission.objects
            .filter(_dual_key_q("student", account, p))
            .select_related("assignment")
            .order_by("-submitted_at")[:20]
        ]

        # Skill Dev is genuinely per-student: SkillSession has its own
        # learner_profile FK (skills/models.py), so unlike watch time these
        # belong in the per-student area rather than the shared block below.
        # Imported lazily — `skills` imports from accounts, so a module-level
        # import here would create a circular import at app load.
        from skills.models import SkillSession
        skill_sessions = (
            SkillSession.objects
            .filter(learner_profile=p)
            .select_related("expert")
            .order_by("-scheduled_for")[:10]
        )
        data["skill_sessions"] = [
            {
                "id": str(s.id),
                "expert": s.expert.display_name() if s.expert else "",
                "status": s.status,
                "scheduled_for": s.scheduled_for,
                "duration_mins": s.duration_mins,
                "amount": s.amount,
            }
            for s in skill_sessions
        ]

        # ── account-scoped: NOT attributable to this student when siblings exist ──
        active_profiles = account.learner_profiles.filter(is_active=True).count()
        live_seconds = (
            LiveSessionAttendance.objects
            .filter(user=account)
            .aggregate(t=Sum("total_seconds"))["t"] or 0
        )
        videos_watched = VideoProgress.objects.filter(student=account).count()
        data["account_scoped"] = {
            "shared_across_profiles": active_profiles,
            "live_attendance_hours": round(live_seconds / 3600, 1),
            "videos_watched": videos_watched,
        }

        return Response(data)

    def patch(self, request, profile_id):
        p = self._get_profile(profile_id)
        # Same validation the account holder's own editor uses, plus student_id
        # (institution-assigned, so admin-only).
        apply_profile_edits(p, request.data, request.FILES, allow_student_id=True)
        try:
            p.save()
        except IntegrityError:
            # student_id is the only unique field reachable here.
            raise ValidationError({
                "student_id": "That student ID is already assigned to another student.",
            })
        return Response(self.get(request, profile_id).data)


class AdminStudentActiveView(APIView):
    """Deactivate / reactivate ONE student without touching the account (which
    may hold their siblings). POST {"is_active": false}."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, profile_id):
        p = (
            LearnerProfile.objects
            .select_related("account")
            .filter(id=profile_id)
            .first()
        )
        if not p:
            raise ValidationError("Student not found.")

        want_active = _bool_param(request.data.get("is_active"))
        if want_active is None:
            raise ValidationError({"is_active": "Required (true/false)."})

        if want_active == p.is_active:
            return Response(_student_row(p))

        if not want_active:
            # Guard 1: User.default_learner_profile() only considers is_active
            # profiles, so zeroing them out leaves the account unable to resolve
            # any profile and breaks its login / profile-select flow. Mirrors the
            # same guard ProfileDetailView.delete already enforces.
            others = p.account.learner_profiles.filter(is_active=True).exclude(id=p.id)
            if not others.exists():
                raise ValidationError(
                    "This is the account's only active student, so deactivating it "
                    "would leave the account with no usable profile. Disable the "
                    "whole account from Users instead."
                )
            with transaction.atomic():
                p.is_active = False
                # Guard 2: the one_default_learner_per_account constraint is
                # conditional on is_default only, NOT on is_active — so a
                # deactivated default would keep the flag and leave the account
                # with a default it can never select. Hand it to a live sibling.
                if p.is_default:
                    p.is_default = False
                    heir = others.order_by("-relationship", "created_at").first()
                    heir.is_default = True
                    p.save(update_fields=["is_active", "is_default"])
                    heir.save(update_fields=["is_default"])
                else:
                    p.save(update_fields=["is_active"])
        else:
            p.is_active = True
            p.save(update_fields=["is_active"])

        p.refresh_from_db()
        return Response(_student_row(p))
