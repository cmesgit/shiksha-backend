# Authorization for locally-stored media files served through
# config.media_views.secure_media_view.
#
# Everything under MEDIA_ROOT used to be served directly by nginx with zero
# auth (`location /media/ { alias ...; }`) — any file URL, once known,
# leaked, or guessed, was permanently and anonymously downloadable
# regardless of what the API endpoint that handed the URL out actually
# checked. Confirmed live on the dev droplet 2026-08-08: this included
# teacher KYC documents (id proofs, certificates), children's profile
# photos, payment/enrollment receipts, and scholarship guardian-
# verification documents — not just study materials.
#
# Paths meant to be genuinely public (marketing images, course thumbnails,
# public bio photos) are tagged PUBLIC in the _RULES table below — nginx's
# own config keeps serving those directly, unauthenticated, for
# CDN-cacheability, and normally never reaches this module at all; the
# PUBLIC branch here exists as defense-in-depth in case nginx's allowlist
# and this table ever drift apart.
#
# Every other prefix MUST resolve through a check function in _RULES, or
# access is denied by default (see `is_authorized`) — an unmapped path is
# a bug to fix here, never a reason to silently allow it through.
#
# nginx's own /media/ location blocks must mirror this table's PUBLIC
# entries exactly (see deploy notes) — nginx has no way to consult this
# Python table directly, so the two are kept in sync by hand.

from django.db.models import Q


def _staff_or(user, ok):
    return bool(user.is_authenticated and (user.is_staff or ok))


def _check_study_material(request, name):
    from materials.models import MaterialFile
    from materials.views import _authorize_subject_materials, TEACHER_UNRESTRICTED

    mf = (
        MaterialFile.objects
        .select_related("material__subject__course")
        .filter(file=name).first()
    )
    if not mf or not mf.material_id:
        return False
    allowed, batch_id = _authorize_subject_materials(request, mf.material.subject)
    if not allowed:
        return False
    if batch_id is TEACHER_UNRESTRICTED:
        return True
    # Same batch isolation the API views enforce (ChapterMaterials/
    # SubjectMaterials): course-wide material (batch IS NULL) is visible to
    # everyone, batch-scoped material only to a student in that batch — the
    # secure-download URL must not be a side door around that.
    return mf.material.batch_id is None or mf.material.batch_id == batch_id


def _check_teacher_application_doc(request, name):
    """teachers/certificates|id_proofs|agreements|skills/videos|skills/files —
    documents submitted as part of a teacher's own application (KYC, signed
    agreement, skill-application media). The applying teacher, or staff
    reviewing the application."""
    from accounts.models import TeacherProfile

    user = request.user
    if not user.is_authenticated:
        return False
    return _staff_or(user, TeacherProfile.objects.filter(user=user).filter(
        Q(qualification_certificate=name) | Q(id_proof_front=name) |
        Q(id_proof_back=name) | Q(signed_agreement=name) |
        Q(skill_supporting_video=name)
    ).exists())


def _check_teacher_application_video(request, name):
    """skills/applications/videos/ — TeacherApplication.intro_video, the
    guest-expert application clip. Unreviewed at submission time, so
    private like the other application documents above (skill_experts/ is
    the separate, already-public post-approval directory photo)."""
    from skills.models import TeacherApplication

    user = request.user
    if not user.is_authenticated:
        return False
    return _staff_or(
        user, TeacherApplication.objects.filter(user=user, intro_video=name).exists()
    )


def _check_learner_photo(request, name):
    """learners/photos/ and learners/avatar/ — a learner's own picture.

    Readable by the owning ACCOUNT (which covers every sibling profile on it),
    by staff, and by a teacher who actually teaches that learner.

    The teacher arm is not a widening for convenience: the teacher-facing
    rosters (courses/views.py `_roster_row`, and the four screens that render
    it — AllStudents, AllStudentDetail, StudentsList, StudentDetail) serve
    these exact avatars, so without it every student photo on the teacher side
    404s no matter how correct the URL is. Scoped through TeachingAssignment so
    it grants only the roster a teacher can already read by name, not the whole
    learner directory.
    """
    from accounts.models import LearnerProfile

    user = request.user
    if not user.is_authenticated:
        return False

    owned = LearnerProfile.objects.filter(account=user).filter(
        Q(profile_photo=name) | Q(avatar_image=name)
    ).exists()
    if _staff_or(user, owned):
        return True

    from enrollments.models import Enrollment

    # Subjects this user actively teaches → the courses behind them → the
    # learners actively enrolled in those courses.
    taught_course_ids = user.teaching_assignments.filter(
        is_active=True,
    ).values_list("subject__course_id", flat=True)
    if not taught_course_ids:
        return False

    return Enrollment.objects.filter(
        course_id__in=taught_course_ids,
        status=Enrollment.STATUS_ACTIVE,
    ).filter(
        Q(learner_profile__profile_photo=name)
        | Q(learner_profile__avatar_image=name)
    ).exists()


def _check_enrollment_receipt(request, name):
    from enrollments.models import Enrollment

    user = request.user
    if not user.is_authenticated:
        return False
    return _staff_or(user, Enrollment.objects.filter(user=user, receipt=name).exists())


def _check_assignment_file(request, name):
    """assignments/files/ — a teacher-uploaded attachment (legacy single
    Assignment.attachment, or the newer AssignmentFile). Any student
    actively enrolled in the assignment's course, or the assigned teacher."""
    from assignments.models import Assignment, AssignmentFile
    from courses.services import teaches_subject
    from enrollments.models import Enrollment

    user = request.user
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True

    af = (
        AssignmentFile.objects
        .select_related("assignment__subject", "assignment__chapter")
        .filter(file=name).first()
    )
    assignment = af.assignment if af else (
        Assignment.objects.select_related("subject", "chapter")
        .filter(attachment=name).first()
    )
    if not assignment:
        return False

    subject = assignment.subject
    if teaches_subject(user, subject):
        return True
    return Enrollment.objects.filter(
        user=user, course=subject.course, status=Enrollment.STATUS_ACTIVE,
    ).exists()


def _check_assignment_submission(request, name):
    """assignments/submissions/ — a student's own submitted file. Only that
    student, or the subject's assigned teacher — classmates never see it."""
    from assignments.models import AssignmentSubmission
    from courses.services import teaches_subject

    user = request.user
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True

    sub = (
        AssignmentSubmission.objects
        .select_related("assignment__subject", "assignment__chapter")
        .filter(submitted_file=name).first()
    )
    if not sub:
        return False
    if sub.student_id == user.id:
        return True
    return teaches_subject(user, sub.assignment.subject)


def _check_chat_attachment(request, name):
    """chat_attachments/<conversation_id>/... — the conversation id is
    already embedded in the path; check the requester is the SPECIFIC
    profile that's a participant, not just any profile on the account.

    Previously scoped on `learner_profile__account=user`, which matched
    ANY learner profile on the requester's account — since Participant
    identity is per-profile (two children on one account chat as separate
    participants), a sibling profile could download another sibling's
    private attachment as long as they shared an account. Resolving the
    actual acting identity via active_identity_from_request() and checking
    participant_for() reuses the exact helper the REST membership checks
    already use, so this can't drift from the API's own membership logic.
    """
    import re
    from chat import services as chat_services
    from chat.models import Conversation, MessageAttachment
    from django.utils import timezone

    user = request.user
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True

    m = re.match(r"^chat_attachments/([0-9a-fA-F-]{36})/", name)
    if not m:
        return False

    kind, obj = chat_services.active_identity_from_request(request)
    if not kind:
        return False
    conv = Conversation.objects.filter(id=m.group(1)).first()
    if conv is None or chat_services.participant_for(conv, kind, obj) is None:
        return False

    # Defense-in-depth: soft_delete_message() now purges the file itself
    # on any deletion path (self-delete, moderator removal, expiry sweep),
    # but this backstop denies access if the row is somehow still there —
    # a file uploaded/deleted before that fix existed, or a race between
    # the expiry sweep and an in-flight request. Staff already returned
    # True above and keep review access to removed content regardless —
    # that's the existing, intentional behavior, not something this closes.
    return not MessageAttachment.objects.filter(file=name).filter(
        Q(message__deleted_at__isnull=False) | Q(expires_at__lte=timezone.now())
    ).exists()


def _check_guardian_doc(request, name):
    """scholarship/guardian_docs/ — the single most sensitive path in this
    table. Identity-verification documents for the Instant Scholarship
    module's guardian/parent verification flow (see scholarship/models.py's
    GuardianVerification docstring for the DPDP Act §9 reasoning). Only the
    submitting parent/guardian account, or staff reviewing it."""
    from scholarship.models import GuardianVerification

    user = request.user
    if not user.is_authenticated:
        return False
    return _staff_or(
        user, GuardianVerification.objects.filter(account=user, manual_document=name).exists()
    )


def _check_counseling_report(request, name):
    """counselors/reports/ — a SessionReport attachment. The student the
    session was for (via the appointment's learner profile), the
    counselor who wrote it, or staff."""
    from counseling.models import SessionReport

    user = request.user
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    report = (
        SessionReport.objects
        .select_related("appointment__learner_profile", "counselor")
        .filter(attachment=name).first()
    )
    if not report:
        return False
    if report.counselor.user_id == user.id:
        return True
    if report.appointment.booked_by_id == user.id:
        return True
    profile = getattr(report.appointment, "learner_profile", None)
    return bool(profile and profile.account_id == user.id)


def _check_skill_payment_doc(request, name):
    """skills/ad_subscriptions/receipts/ + skills/payments/receipts/ —
    payment-proof uploads. The paying learner/expert, or staff."""
    from skills.subscription_models import ExpertAdSubscription
    from skills.payment_models import SkillPaymentRequest

    user = request.user
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    if ExpertAdSubscription.objects.filter(
        expert__teacher_profile__user=user, receipt=name,
    ).exists():
        return True
    return SkillPaymentRequest.objects.filter(
        learner_profile__account=user, receipt=name,
    ).exists()


def _check_explore_document(request, name):
    """explore/documents/ — documents.Document.file, the Explore Library.
    Mirrors DocumentDetailView/RecordDownloadView exactly: AllowAny, gated
    only by is_removed (moderator soft-hide) — there's no per-document
    owner/visibility restriction, every non-removed document is public."""
    from documents.models import Document

    return Document.objects.filter(file=name, is_removed=False).exists()


def _check_forum_attachment(request, name):
    """forum/attachments/ — had NO rule at all, so every forum attachment
    404'd for every non-staff user, including whoever just uploaded it
    (ListThreadsView/ThreadDetailView are AllowAny, so the URLs were handed
    out to everyone; nobody but staff could actually fetch the bytes).
    Mirrors the thread's own visibility for everyone else: forum reads are
    AllowAny, gated only by the post's own is_removed soft-hide — there's
    no separate attachment-level or space-level restriction to apply.
    Staff additionally see removed posts' attachments (moderation review),
    matching _check_chat_attachment / _check_guardian_doc etc. above.
    NOT using _staff_or() here — it ANDs in is_authenticated even for the
    "ok" branch, which would wrongly deny the anonymous readers this AllowAny
    surface must keep serving."""
    from forum.models import Attachment

    user = request.user
    if user.is_authenticated and user.is_staff:
        return True
    return Attachment.objects.filter(file=name, post__is_removed=False).exists()


def _check_session_file(request, name):
    """session_files/ — shared by SessionFile (group sessions) and
    PrivateSessionFile (1:1 sessions); same upload_to, two different models,
    so both have to be checked. Had NO rule at all, so every file shared in
    a live class or private session 404'd for every non-staff user —
    including the teacher/student who just uploaded or was in the room.
    Mirrors the exact authorization the live views themselves use
    (sessions_app/live_files_views.py's _in_room / _is_private_session_participant)
    rather than reinventing it, so the two can't silently drift apart."""
    from sessions_app.models import SessionFile, PrivateSessionFile
    from sessions_app.live_files_views import _in_room
    from sessions_app.views import _is_private_session_participant

    user = request.user
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True

    group_file = SessionFile.objects.select_related("session").filter(file=name).first()
    if group_file:
        session = group_file.session
        return _in_room(user, session) or user.id == session.host_id

    private_file = PrivateSessionFile.objects.select_related("session").filter(file=name).first()
    if private_file:
        return _is_private_session_participant(private_file.session, user)

    return False


# A single sentinel, not a check function — matching this prefix means
# "genuinely public, no auth needed" and short-circuits before any DB work.
PUBLIC = object()

# (prefix, check_fn_or_PUBLIC). Deliberately NOT consulted in declaration
# order — see _ordered_rules() below. This matters because some public
# prefixes are strict PARENTS of a private one (bare "teachers/" — the
# public bio photo — contains "teachers/certificates/", which must stay
# private): checking PUBLIC_PREFIXES and _PRIVATE_CHECKS as two separate
# passes (the original version of this module did exactly this) meant
# `name.startswith(PUBLIC_PREFIXES)` matched "teachers/certificates/x.pdf"
# against the bare "teachers/" public prefix and returned public BEFORE
# the private check for "teachers/certificates/" ever ran — silently
# exposing every teacher's KYC documents the moment the bare "teachers/"
# prefix was added for the bio photo. Both are folded into one table now,
# resolved strictly by prefix length, so specificity always wins regardless
# of which was written first or which list it lives in.
_RULES = (
    ("content/", PUBLIC),
    ("subjects/", PUBLIC),
    ("courses/thumbnails/", PUBLIC),
    ("boards/logos/", PUBLIC),
    ("counselors/photos/", PUBLIC),
    ("counseling/guides/", PUBLIC),
    ("skills/marketing/", PUBLIC),
    ("skills/courses/covers/", PUBLIC),
    ("skills/categories/", PUBLIC),
    ("skills/experts/", PUBLIC),
    ("teachers/skills/images/", PUBLIC),  # approved "supporting image" —
                                            # the *application* video below
                                            # stays private until reviewed
    ("teachers/", PUBLIC),  # bio photo — MUST be shorter than every
                              # teachers/* private prefix below so those
                              # win on specificity, never this one
    ("study_materials/", _check_study_material),
    ("teachers/certificates/", _check_teacher_application_doc),
    ("teachers/id_proofs/", _check_teacher_application_doc),
    ("teachers/agreements/", _check_teacher_application_doc),
    ("teachers/skills/videos/", _check_teacher_application_doc),
    ("teachers/skills/files/", _check_teacher_application_doc),
    ("skills/applications/videos/", _check_teacher_application_video),
    ("learners/photos/", _check_learner_photo),
    ("learners/avatar/", _check_learner_photo),
    ("enrollment_receipts/", _check_enrollment_receipt),
    ("assignments/submissions/", _check_assignment_submission),
    ("assignments/files/", _check_assignment_file),
    ("chat_attachments/", _check_chat_attachment),
    ("scholarship/guardian_docs/", _check_guardian_doc),
    ("counselors/reports/", _check_counseling_report),
    ("skills/ad_subscriptions/receipts/", _check_skill_payment_doc),
    ("skills/payments/receipts/", _check_skill_payment_doc),
    ("explore/documents/", _check_explore_document),
    ("forum/attachments/", _check_forum_attachment),
    ("session_files/", _check_session_file),
    # The BLANK agreement letter an admin imported for a version. Genuinely
    # public: a prospective faculty member has to read and download it during
    # signup, BEFORE their account exists, so this cannot require auth. It is
    # a template containing nobody's data. NOT to be confused with
    # "teachers/agreements/" above — that is the SIGNED copy an individual
    # faculty member uploaded, and stays owner-or-staff only.
    ("agreements/letters/", PUBLIC),
)

# Sorted longest-prefix-first once at import time, so correctness never
# depends on the declaration order above (which is grouped for
# readability — public block first, then private — not by length).
_ORDERED_RULES = sorted(_RULES, key=lambda rule: -len(rule[0]))


def _lookup(name):
    for prefix, rule in _ORDERED_RULES:
        if name.startswith(prefix):
            return rule
    return None


def is_public(name):
    return _lookup(name) is PUBLIC


def is_authorized(request, name):
    """True iff `request.user` may read this media path. Deny-by-default —
    an unmapped prefix (forum/ attachments and a few unidentified legacy
    paths as of 2026-08-13 — see MEDIA_SECURITY_TODO.md) resolves to
    staff-only until someone adds a real rule for it above."""
    rule = _lookup(name)
    if rule is PUBLIC:
        return True  # secure_media_view only exists for defense-in-depth
                       # here — nginx serves public paths directly and
                       # never reaches this view for them.
    if rule is not None:
        return rule(request, name)
    return bool(request.user.is_authenticated and request.user.is_staff)
