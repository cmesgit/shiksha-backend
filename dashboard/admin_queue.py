"""GET /api/dashboard/admin/queue/ — everything waiting on an admin, counted once.

design_handoff_admin_counsellor Phase 2. The Admin v2 Home screen opens on
"N things are waiting for you"; this is the N, and its breakdown.

Mounted in dashboard/ for the same reason the academy rollup is: it belongs to
no single app. This one spans accounts, counseling, enrollments, quizzes,
scholarship, chat, skills, courses, content and documents — ten apps.

WHY THIS EXISTS RATHER THAN THE FRONTEND SUMMING EXISTING ENDPOINTS
───────────────────────────────────────────────────────────────────
That is what `Admin-dashboard` does today, and it is wrong in three ways that
are invisible until you look:

  * `Overview.jsx:64` calls `getEnrollmentRequests()` with NO status, and
    `enrollments/views.py:70-76` only filters when one is given — so the badge
    counts approved and rejected rows too.
  * It then `len()`s the first page only (`page_size=50`), so the same number
    is simultaneously over- and under-counted.
  * `chat/views.py:827,983` hard-slice at 200 and `skills/admin_views.py:152`
    at 300, so any badge built by measuring those payloads silently stops
    counting at the slice.

Every count below is a `.count()` on an explicit filter. None of them loads
rows.

AUTHORISATION IS NOT UNIFORM ACROSS THESE APPS — see `_RESEARCH_KINDS`.

Each count runs in its own try/except, defaulting to 0, following
`AdminStatsView` (`accounts/views.py:1494-1525`): one unmigrated app must not
500 the admin's home screen.
"""
import logging

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin

logger = logging.getLogger(__name__)

# Kinds whose screen is gated on the ADMIN *role* (or superuser) rather than
# on `is_staff`, which is what IsAdmin checks. `documents/research_workflow.py:25`
# defines its own `is_research_admin`, so a staff account without the role sees
# a non-zero tile here and a 403 the moment it clicks. Those kinds are omitted
# for such an account instead — reported under `unavailable`, not as a zero,
# because "nothing to do" and "not yours to do" are different facts.
_RESEARCH_KINDS = {"research_paper", "research_case", "research_reviewer"}

# ── Which kinds may be actioned INLINE from the queue ───────────────────
#
# The criterion is not convenience — it is whether the SERVER re-checks that
# the row is still actionable before acting. An inline list is the one place
# where two admins are most likely to click the same row seconds apart, so the
# endpoint behind it has to be the thing that says no.
#
#   teacher       accounts/views.py:1684    filters on TRACK_PENDING → 404
#   admission     enrollments/serializers.py:332  "already been reviewed" → 400
#   skill_course  skills/course_views.py:381      not in submitted queue → 404
#   counsellor    counseling/views.py's AdminApplicationActionView re-reads the
#                 row under select_for_update() and 400s when the action does
#                 not apply to its current status (LEGAL_FROM). Guarded
#                 2026-11-13; it used to be the worst of the excluded set, as
#                 a second Approve emailed the applicant twice.
#
# ⚠ DELIBERATELY EXCLUDED, each for a specific reason — do not "finish the
# set" without fixing the endpoint first:
#
#   research_*    ResearchReviewerAdminView is a TOGGLE by design (the same
#                 endpoint serves "approve" and "suspend access"), so there is
#                 no "already actioned" state for it to reject. Declining also
#                 cascades: it revokes live assignments and rolls papers back
#                 to SUBMITTED. Too much consequence for a one-click row.
#   question      quizzes/views.py:3472 is a plain get_object_or_404 with no
#                 pending re-check, and `request_changes` REQUIRES feedback —
#                 a text field the queue row has no room for.
#
# These kinds still show their rows; they just link to the screen that owns
# the decision instead of offering a button here.
_INLINE_ACTIONABLE = {"teacher", "admission", "skill_course", "counsellor"}


def _count(fn):
    """Run one counter, swallowing anything it raises."""
    try:
        return int(fn())
    except Exception:
        logger.exception("admin queue: a counter failed")
        return 0


# How many example rows each kind contributes. The screen shows these and
# links to the full list; it is a preview, not a pager. Kept small because
# every kind pays for its own query.
ROW_LIMIT = 5


def _rows(fn, limit):
    """Run one row builder, swallowing anything it raises.

    A kind whose rows fail still shows its COUNT — the number comes from a
    separate query. Losing the preview is a much smaller failure than losing
    the fact that something is waiting, so these degrade independently.
    """
    try:
        return list(fn(limit))
    except Exception:
        logger.exception("admin queue: a row builder failed")
        return []


def _who(user):
    """A person's name for an admin list, falling back to the login."""
    if user is None:
        return "Someone"
    try:
        from accounts.display import display_name_for
        name = display_name_for(user)
        if name:
            return name
    except Exception:
        pass
    return getattr(user, "email", None) or getattr(user, "username", "Someone")


# ── The counters ────────────────────────────────────────────────────────
# Each returns an int. Kept as small named functions rather than one giant
# dict literal so the filter that defines "waiting" is readable next to the
# evidence for it.


def _teachers():
    from accounts.models import TeacherProfile
    # NOT is_approved / teacher_type — accounts/models.py:1147-1165 explains
    # that both lie (skill auto-approves; teacher_type counts a pending track).
    return TeacherProfile.objects.filter(
        academy_status=TeacherProfile.TRACK_PENDING
    ).count()


def _counsellors():
    from counseling.models import CounselorProfile
    return CounselorProfile.objects.filter(
        status=CounselorProfile.STATUS_PENDING
    ).count()


def _admissions():
    from enrollments.models import EnrollmentRequest
    # The uppercase constant: enrollments/views.py upper()s the query param,
    # and the stored value is "PENDING".
    return EnrollmentRequest.objects.filter(
        status=EnrollmentRequest.STATUS_PENDING
    ).count()


def _questions():
    from quizzes.models import Question
    # ⚠ quiz__isnull=False is MANDATORY. bank_state defaults to SUGGESTED and
    # Question.quiz went nullable with the public Quiz Hub, so without this
    # every admin-authored and bulk-imported standalone question joins the
    # teachers' review queue (quizzes/views.py:3314-3325).
    return (
        Question.objects
        .exclude(bank_state=Question.BANK_STATE_PRIVATE)
        .filter(bank_state=Question.BANK_STATE_SUGGESTED, quiz__isnull=False)
        .count()
    )


def _scholarship_flagged():
    from scholarship.models import ExamSession
    # "Open" is review_status == "" — an EMPTY STRING, not null. The field is
    # blank=True with choices only cleared/voided, so __isnull=True finds none.
    return ExamSession.objects.filter(
        flagged_for_review=True, review_status=""
    ).count()


def _scholarship_verifications():
    from scholarship.models import GuardianVerification
    return GuardianVerification.objects.filter(
        status=GuardianVerification.STATUS_PENDING
    ).count()


def _support():
    from chat.models import SupportTicket
    # OPEN only. Assigning a ticket moves it to IN_PROGRESS (chat/views.py:1003),
    # which is someone's work in hand rather than an unclaimed queue.
    return SupportTicket.objects.filter(status=SupportTicket.STATUS_OPEN).count()


def _reported_chats():
    from chat.models import Report
    # STATUS_REVIEWED exists in the vocabulary but nothing ever sets it —
    # resolution writes ACTION_TAKEN or DISMISSED — so OPEN is complete.
    return Report.objects.filter(status=Report.STATUS_OPEN).count()


def _skill_courses():
    from skills.course_models import SkillCourse
    return SkillCourse.objects.filter(status=SkillCourse.STATUS_SUBMITTED).count()


def _recordings_failed():
    from courses.models_recordings import SessionRecording
    # 5 == "Error". There is no named constant anywhere — STATUS_CHOICES is a
    # bare list of integer tuples (courses/models_recordings.py:90-99), so this
    # magic number is the codebase's own vocabulary, not an invention here.
    return SessionRecording.objects.filter(status=5).count()


def _ad_payments():
    from skills.subscription_models import ExpertAdSubscription
    # ⚠ SUBMITTED only, NOT the view's default of [SUBMITTED, PENDING].
    # PENDING means "created, awaiting payment proof" — waiting on the EXPERT.
    # It is also the model default, and rejecting pushes a row back into it
    # (skills/subscription_views.py:208), so PENDING grows forever and is
    # mostly not admin work. Overview.jsx's badge has this bug today.
    return ExpertAdSubscription.objects.filter(
        status=ExpertAdSubscription.STATUS_SUBMITTED
    ).count()


def _content_in_review():
    """Everything in the CMS sitting at REVIEW, across all eight models.

    REVIEW is the only state that means "waiting on an admin":
      * DRAFT waits on its author.
      * PUBLISHED with publish_at in the future is SCHEDULED — the whole
        mechanism, no cron (content/models.py:79-81). Nothing is stuck.
      * PUBLISHED with publish_at in the past is already live.

    Counted directly rather than through GET /content/admin/inbox/, because
    that view is behind IsStudioEditor and 403s while content_studio_enabled
    is off (content/permissions.py:13-38) — the count would vanish for an
    admin who can still reach the screens at /content.
    """
    from content.models import PublishStatus
    from content import models as cm

    names = [
        "BlogPost", "CurrentAffair", "FAQItem", "Announcement",
        "ShowcaseCourse", "HomeContentBlock", "HomeListItem", "HomeFloater",
    ]
    total = 0
    for name in names:
        model = getattr(cm, name, None)
        if model is None:
            continue
        total += model.objects.filter(status=PublishStatus.REVIEW).count()
    return total


def _research_papers():
    from documents.models import ResearchSubmission
    # The label on this state is literally "Awaiting admin decision".
    return ResearchSubmission.objects.filter(
        status=ResearchSubmission.Status.APPROVAL
    ).count()


def _research_cases():
    from documents.models import ResearchCase
    return ResearchCase.objects.filter(status="open").count()


def _research_reviewers():
    from documents.models import ResearchReviewer
    return ResearchReviewer.objects.filter(approved=False).count()


# ── The row builders ────────────────────────────────────────────────────
# Each takes a limit and returns [{title, subtitle, at}]. `at` is an ISO
# timestamp or None — the screen shows "how long it has been waiting", and a
# kind with no timestamp field says nothing rather than inventing one.
#
# ⚠ Every one of these slices BEFORE evaluating and select_related()s what it
# reads. A preview that fires one query per row would make the home screen
# slower than the four badge calls it replaced.


def _iso(dt):
    return dt.isoformat() if dt else None


def _rows_teachers(limit):
    from accounts.models import TeacherProfile
    qs = (TeacherProfile.objects
          .filter(academy_status=TeacherProfile.TRACK_PENDING)
          .select_related("user")[:limit])
    # ⚠ The action endpoint is keyed on the TeacherProfile pk (an int), NOT
    # the User pk (a UUID) — accounts/views.py:1683 and
    # accounts/serializers.py:774-780 both say so explicitly, because the
    # serializer emits both `id` and `user_id`.
    return [{"id": str(t.pk),
             "title": _who(t.user),
             "subtitle": t.qualification or "Applied to teach",
             "at": None} for t in qs]


def _rows_counsellors(limit):
    from counseling.models import CounselorProfile
    qs = (CounselorProfile.objects
          .filter(status=CounselorProfile.STATUS_PENDING)
          .select_related("user")[:limit])
    # ⚠ `id` is the CounselorProfile pk, which is what
    # /counseling/admin/applications/<id>/action/ takes — NOT the User pk.
    # Unlike the teacher endpoint there is no UUID/int mismatch to save a
    # mix-up here, so a wrong id would action a real but different applicant.
    return [{"id": str(c.pk),
             "title": c.display_name or _who(c.user),
             "subtitle": (c.qualifications or "Applied to counsel")[:90],
             "at": None} for c in qs]


def _rows_admissions(limit):
    from enrollments.models import EnrollmentRequest
    qs = (EnrollmentRequest.objects
          .filter(status=EnrollmentRequest.STATUS_PENDING)
          .select_related("user", "course", "batch")
          .order_by("payment_date")[:limit])
    out = []
    for r in qs:
        bits = [getattr(r.course, "title", None) or "a course"]
        if r.batch_id:
            bits.append(getattr(r.batch, "code", "") or "")
        out.append({"id": str(r.pk),
                    "title": _who(r.user),
                    "subtitle": " · ".join(b for b in bits if b),
                    "at": _iso(r.payment_date)})
    return out


def _rows_questions(limit):
    from quizzes.models import Question
    qs = (Question.objects
          .exclude(bank_state=Question.BANK_STATE_PRIVATE)
          .filter(bank_state=Question.BANK_STATE_SUGGESTED, quiz__isnull=False)
          .select_related("quiz")[:limit])
    return [{"title": (q.text or "Untitled question")[:90],
             "subtitle": getattr(q.quiz, "title", "") or "Suggested for the bank",
             "at": None} for q in qs]


def _rows_scholarship_verifications(limit):
    from scholarship.models import GuardianVerification
    qs = (GuardianVerification.objects
          .filter(status=GuardianVerification.STATUS_PENDING)
          .select_related("account")[:limit])
    return [{"title": g.verified_adult_name or _who(g.account),
             "subtitle": f"{g.method} verification",
             "at": _iso(g.consent_given_at)} for g in qs]


def _rows_scholarship_flagged(limit):
    from scholarship.models import ExamSession
    qs = (ExamSession.objects
          .filter(flagged_for_review=True, review_status="")
          .select_related("learner_profile", "course")[:limit])
    return [{"title": getattr(s.learner_profile, "display_name", None) or "A student",
             "subtitle": (getattr(s.course, "title", "") or "Scholarship test")
                         + f" · {s.tab_switch_count} tab switches",
             "at": _iso(s.submitted_at or s.started_at)} for s in qs]


def _rows_support(limit):
    from chat.models import SupportTicket
    qs = (SupportTicket.objects
          .filter(status=SupportTicket.STATUS_OPEN)
          .order_by("created_at")[:limit])
    return [{"title": t.subject or "Support request",
             "subtitle": t.category or t.requester_kind or "",
             "at": _iso(t.created_at)} for t in qs]


def _rows_reported_chats(limit):
    from chat.models import Report
    qs = (Report.objects
          .filter(status=Report.STATUS_OPEN)
          .order_by("created_at")[:limit])
    return [{"title": f"Reported: {r.reason or 'no reason given'}",
             "subtitle": (r.detail or "")[:90],
             "at": _iso(r.created_at)} for r in qs]


def _rows_skill_courses(limit):
    from skills.course_models import SkillCourse
    qs = (SkillCourse.objects
          .filter(status=SkillCourse.STATUS_SUBMITTED)
          .select_related("teacher_profile", "teacher_profile__user")[:limit])
    return [{"id": str(c.pk),
             "title": c.title or "Untitled course",
             "subtitle": _who(getattr(c.teacher_profile, "user", None)),
             "at": None} for c in qs]


def _rows_recordings(limit):
    from courses.models_recordings import SessionRecording
    qs = (SessionRecording.objects
          .filter(status=5)
          .select_related("subject")
          .order_by("-created_at")[:limit])
    return [{"title": r.title or "Untitled recording",
             "subtitle": (getattr(r.subject, "name", "") or "") + " · failed to process",
             "at": _iso(r.created_at)} for r in qs]


def _rows_ad_payments(limit):
    from skills.subscription_models import ExpertAdSubscription
    qs = (ExpertAdSubscription.objects
          .filter(status=ExpertAdSubscription.STATUS_SUBMITTED)
          .select_related("expert", "expert__teacher_profile__user")
          .order_by("created_at")[:limit])
    return [{"title": _who(getattr(getattr(s.expert, "teacher_profile", None), "user", None)),
             "subtitle": f"{s.plan} · {s.amount}",
             "at": _iso(s.created_at)} for s in qs]


def _rows_content(limit):
    """Rows across the eight reviewable CMS models.

    They do not share a title field — Announcement has `message`, the home
    blocks have `heading` — so each is read through a small accessor rather
    than assuming one shape. Sliced per model, then trimmed to `limit`.
    """
    from content.models import PublishStatus
    from content import models as cm

    specs = [
        ("BlogPost", "title", "Post"),
        ("CurrentAffair", "title", "Current affair"),
        ("FAQItem", "question", "Question"),
        ("Announcement", "message", "Notice"),
        ("ShowcaseCourse", "title", "Course card"),
        ("HomeContentBlock", "heading", "Home block"),
        ("HomeListItem", "title", "Home list item"),
        ("HomeFloater", "title", "Home floater"),
    ]
    out = []
    for name, field, label in specs:
        model = getattr(cm, name, None)
        if model is None:
            continue
        for obj in model.objects.filter(status=PublishStatus.REVIEW)[:limit]:
            out.append({
                "title": (str(getattr(obj, field, "") or "Untitled"))[:90],
                "subtitle": label,
                "at": _iso(getattr(obj, "updated_at", None)),
            })
            if len(out) >= limit:
                return out
    return out


def _rows_research_papers(limit):
    from documents.models import ResearchSubmission
    qs = (ResearchSubmission.objects
          .filter(status=ResearchSubmission.Status.APPROVAL)
          .select_related("owner")[:limit])
    return [{"title": (s.title or "Untitled paper")[:90],
             "subtitle": s.subject or _who(s.owner),
             "at": None} for s in qs]


def _rows_research_cases(limit):
    from documents.models import ResearchCase
    qs = (ResearchCase.objects.filter(status="open")
          .select_related("submission")
          .order_by("created_at")[:limit])
    return [{"title": f"{c.kind}: {getattr(c.submission, 'title', 'a paper')}"[:90],
             "subtitle": (c.reason or "")[:90],
             "at": _iso(c.created_at)} for c in qs]


def _rows_research_reviewers(limit):
    from documents.models import ResearchReviewer
    qs = (ResearchReviewer.objects.filter(approved=False)
          .select_related("user")[:limit])
    return [{"title": _who(r.user),
             "subtitle": (r.expertise or "")[:90],
             "at": _iso(getattr(r, "updated_at", None))} for r in qs]


# kind, label, the screen that actually resolves it, which v2 area it sits in
_KINDS = [
    ("teacher",        "Teacher",        "People › Teacher applications", "people",      "/approvals",                       _teachers,                  _rows_teachers),
    ("counsellor",     "Counsellor",     "People › Counsellors",          "people",      "/counselor-approvals",             _counsellors,               _rows_counsellors),
    ("admission",      "Admission",      "Courses › Admissions",          "courses",     "/enrollment-requests",             _admissions,                _rows_admissions),
    ("question",       "Question",       "Questions › To review",         "questions",   "/question-bank/review",            _questions,                 _rows_questions),
    ("scholarship",    "Scholarship",    "Scholarship › Checks to do",    "scholarship", "/scholarship?tab=verifications",   _scholarship_verifications, _rows_scholarship_verifications),
    ("exam_flag",      "Flagged test",   "Scholarship › Tests taken",     "scholarship", "/scholarship?tab=sessions",        _scholarship_flagged,       _rows_scholarship_flagged),
    ("help_request",   "Help request",   "Help desk › Help requests",     "help",        "/communication/support",           _support,                   _rows_support),
    ("reported_chat",  "Reported chat",  "Help desk › Reported chats",    "help",        "/communication/reports",           _reported_chats,            _rows_reported_chats),
    ("skill_course",   "Skill course",   "Courses › Skill course review", "courses",     "/skill-courses",                   _skill_courses,             _rows_skill_courses),
    ("recording",      "Recording",      "Live classes › Recordings",     "live",        "/recordings",                      _recordings_failed,         _rows_recordings),
    ("ad_payment",     "Ad payment",     "Website › Ad payments",         "website",     "/ad-subscriptions",                _ad_payments,               _rows_ad_payments),
    ("content",        "Post or notice", "Website › Posts and articles",  "website",     "/content?tab=blogs",               _content_in_review,         _rows_content),
    ("research_paper", "Research paper", "Website › Research",            "website",     "/research",                        _research_papers,           _rows_research_papers),
    ("research_case",  "Research case",  "Website › Research",            "website",     "/research",                        _research_cases,            _rows_research_cases),
    ("research_reviewer", "Reviewer application", "Website › Research",   "website",     "/research",                        _research_reviewers,        _rows_research_reviewers),
]

# Deliberately absent, because the state they would count does not exist:
#   * Study material — materials.StudyMaterial has NO approval/publish field
#     at all; material is live on upload (dashboard/teacher_resources.py:86-95).
#   * Pictures — content.ContentImage has no moderation state, and SkillCMS's
#     "Media Moderation" tab is browse-and-overwrite over ALL experts and
#     courses, not a queue.
#   * Skill experts — the skill track AUTO-APPROVES
#     (accounts/signup_serializer.py:373-378); /skill-experts is a roster.
# Showing them as a permanent 0 would imply a queue that cannot exist.


class AdminQueueView(APIView):
    """GET /api/dashboard/admin/queue/

    Returns:
        {
          "total": 29,
          "generated_at": "...",
          "groups": [
            {"kind": "teacher", "label": "Teacher", "count": 3,
             "where": "People › Teacher applications",
             "area": "people", "url": "/approvals",
             "items": [{"title": "Meera Gokhale",
                        "subtitle": "M.Sc Chemistry", "at": null}]}
          ],
          "unavailable": [{"kind": "research_paper", "reason": "..."}]
        }

    `items` is a PREVIEW capped at ROW_LIMIT, never the full list — `count` is
    the real number and the two will disagree whenever count > ROW_LIMIT. The
    screen must say "and N more", not paginate this. `?items=0` omits them.

    `groups` carries only non-zero kinds — the screen renders a chip per kind
    and an empty chip is noise. `total` is their sum, so it is always the sum
    of what is shown.
    """

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        user = request.user
        research_ok = bool(
            getattr(user, "is_superuser", False)
            or (hasattr(user, "has_role") and user.has_role("ADMIN"))
        )

        # ?items=0 returns counts only — for the nav badges, which need the
        # numbers on every page load and none of the previews.
        want_items = request.query_params.get("items") != "0"

        groups, unavailable = [], []
        for kind, label, where, area, url, counter, row_fn in _KINDS:
            if kind in _RESEARCH_KINDS and not research_ok:
                unavailable.append({
                    "kind": kind,
                    "reason": "Needs the ADMIN role, not just staff access.",
                })
                continue
            n = _count(counter)
            if n:
                groups.append({
                    "kind": kind, "label": label, "count": n,
                    "where": where, "area": area, "url": url,
                    # Rows degrade independently of the count — see _rows().
                    "items": _rows(row_fn, ROW_LIMIT) if want_items else [],
                    # Whether the client may offer Approve/Decline on the row
                    # itself. See _INLINE_ACTIONABLE for why only three
                    # qualify — the client must not widen this on its own.
                    "can_act": kind in _INLINE_ACTIONABLE,
                })

        groups.sort(key=lambda g: -g["count"])
        return Response({
            "total": sum(g["count"] for g in groups),
            "generated_at": timezone.now().isoformat(),
            "groups": groups,
            "unavailable": unavailable,
        })
