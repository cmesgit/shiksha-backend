# ============================================================
# BACKEND — activity/signals.py  (FULL REPLACEMENT)
# ============================================================
#
# WHAT CHANGED vs the previous version
# ────────────────────────────────────
# 1. PROFILE ISOLATION — _bulk_notify_students already iterated the
#    Enrollment rows, which carry learner_profile; it just threw that
#    away. Each Activity row is now written with
#        learner_profile = enrollment.learner_profile
#        audience        = LEARNER
#    and teacher rows with audience=TEACHER, learner_profile=NULL.
#    Two children on one parent email finally get separate feeds, and
#    an account enrolled twice (two profiles, same course) correctly
#    gets one row per profile instead of one ambiguous row.
#
# 2. ONE WS SHAPE — the old WS payloads were a third, hand-rolled
#    vocabulary ({"type": "assignment"|"quiz"|"submission"|
#    "live_session"}, id = the *assignment's* uuid, no is_read, no
#    created_at). Merged into the REST feed on the frontend they
#    duplicated (different ids for the same event) and could never be
#    marked read (PATCH /activity/feed/<assignment_id>/read/ → 404).
#    Pushes now carry the SERIALIZED ACTIVITY ROW — same id, same
#    fields as GET /activity/feed/ — so dedupe and mark-read just work.
#    A `subtype` extra is preserved for quiz submissions (the teacher
#    bell routes on it).
#
# 3. TARGETED PUSH — every push includes audience +
#    learner_profile_id; accounts/consumers.py drops events that don't
#    match the connection's context/profile, so a teacher tab never
#    flashes a child's assignment and vice versa.

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from assignments.models import Assignment, AssignmentSubmission
from enrollments.models import Enrollment
from livestream.models import LiveSession
from livestream.services.notifications import push_ws_notification
from quizzes.models import Quiz, QuizAttempt

from .models import Activity
from .serializers import ActivitySerializer


# =====================================================
# HELPERS
# =====================================================

def _display_name(user):
    """Best display name for an account: its default learner profile's
    full name, else email. (AssignmentSubmission.student is the ACCOUNT —
    see AUDIT.md → known model gaps.)"""
    try:
        lp = user.default_learner_profile()
        if lp and lp.full_name:
            return lp.full_name
        if lp and lp.display_name:
            return lp.display_name
    except Exception:
        pass
    return user.email


def _ws_payload(activity, extra=None):
    """The WS frame body == the REST feed row, plus optional extras.
    Serializing the saved row guarantees the frontend sees ONE shape."""
    data = dict(ActivitySerializer(activity).data)
    if extra:
        data.update(extra)
    return data


def _enrollments_for(course, batch_id):
    """Active enrollments that should be told about a course item.

    Mirrors the STUDENT-VISIBILITY rule exactly, which is the only correct
    basis for a notification: tell precisely the people who can see the
    thing. Two halves, both load-bearing:

      · batch_id is None  → the item is course-wide; everyone gets it.
      · batch_id is set   → that batch, PLUS enrollments with NO batch.
        The un-batched half is not sloppiness. assignments/views.py:202-210
        shows a student who has not been placed in a cohort is shown EVERY
        assignment in the course, because we cannot tell which cohort
        applies to them — so they must be notified about batch-scoped items
        too, or they would see an assignment appear with no notification.

    This is the same shape notifications/tasks.py:224 already uses for
    livestream reminders.

    Before this existed, assignment_created and quiz_published filtered on
    course alone, so a batch-scoped assignment notified EVERY batch. That
    was survivable as an Activity row; as a durable notification (with
    push, and email/SMS for some verbs) it is not.
    """
    qs = (Enrollment.objects
          .filter(course=course, status=Enrollment.STATUS_ACTIVE)
          .select_related("user", "learner_profile"))
    if batch_id is not None:
        qs = qs.filter(Q(batch_id=batch_id) | Q(batch__isnull=True))
    return qs


def _bulk_notify_students(enrollments, obj, activity_type, title, due_date,
                          subject_id, subject_name, extra=None, verb=None,
                          link_url=""):
    """One Activity per (account, learner_profile) enrollment row, then a
    targeted WS push per row. UUID pks are generated client-side, so the
    objects passed to bulk_create already have ids we can serialize.

    `verb` opts the batch into DURABLE notifications.Notification rows as
    well. Until now this whole path was Activity + a fire-and-forget WS
    frame, so a student who was offline when an assignment or quiz was
    posted never found out — the same gap that was just closed for session
    bookings. Call sites without a notifications/policy.py row leave it
    None and keep their existing Activity-only behaviour.

    push_ws=False on the notify() below: the loop already pushes a frame
    per row, and notify()'s own frame carries a different id (integer pk vs
    Activity UUID), so both would render as separate bell items.
    """
    content_type = ContentType.objects.get_for_model(obj)
    rows = list(enrollments)  # evaluate once

    now = timezone.now()
    activities = [
        Activity(
            user=e.user,
            learner_profile=e.learner_profile,        # ← the isolation fix
            audience=Activity.AUDIENCE_LEARNER,
            type=activity_type,
            title=title,
            due_date=due_date,
            subject_id=subject_id,
            subject_name=subject_name,
            content_type=content_type,
            object_id=obj.id,
        )
        for e in rows
    ]
    Activity.objects.bulk_create(activities)

    if verb:
        from notifications.services import notify
        for e in rows:
            # audience_identity keeps a sibling's assignment off the other
            # child's bell — the same per-profile scope Activity gets from
            # learner_profile above. A legacy enrollment with no profile
            # falls back to account-wide, matching the Activity row.
            notify(
                recipient=e.user,
                verb=verb,
                title=title,
                link_url=link_url,
                payload={"object_id": str(obj.id),
                         "subject_id": str(subject_id) if subject_id else ""},
                audience_identity=(f"L:{e.learner_profile_id}"
                                   if e.learner_profile_id else ""),
                learner_profile=e.learner_profile,
                push_ws=False,
            )

    for act in activities:
        # bulk_create skips auto_now_add on some backends' returned attrs;
        # stamp a value so the WS frame always has created_at.
        if act.created_at is None:
            act.created_at = now
        push_ws_notification(act.user_id, _ws_payload(act, extra))


def _notify_teacher(teacher, obj, activity_type, title, due_date,
                    subject_id, subject_name, extra=None,
                    verb=None, link_url=""):
    content_type = ContentType.objects.get_for_model(obj)

    act = Activity.objects.create(
        user=teacher,
        learner_profile=None,
        audience=Activity.AUDIENCE_TEACHER,
        type=activity_type,
        title=title,
        due_date=due_date,
        subject_id=subject_id,
        subject_name=subject_name,
        content_type=content_type,
        object_id=obj.id,
    )

    # A durable Notification alongside the Activity row, so the event also
    # reaches the Communication Center and whatever email/push the verb's
    # policy allows. Without it a teacher who was offline when a student
    # submitted learned about it only if they happened to scroll the feed —
    # no email, no push, nothing in the Comm Center list. Optional (verb=None
    # keeps the old Activity-only behaviour) so existing callers are
    # unaffected until they opt in.
    if verb:
        from notifications.services import notify
        notify(
            recipient=teacher,
            actor=None,
            verb=verb,
            title=title,
            link_url=link_url,
            payload={"object_id": str(obj.id), "subject_id": str(subject_id)},
            push_ws=False,   # the Activity frame below already carries it
        )

    push_ws_notification(teacher.id, _ws_payload(act, extra))


# =====================================================
# ASSIGNMENT CREATED → notify enrolled learner profiles
# =====================================================

@receiver(pre_save, sender=Assignment)
def cache_assignment_published_state(sender, instance, **kwargs):
    # Same shape as cache_quiz_published_state: post_save re-queries the
    # already-saved row, so the False→True transition can only be observed
    # by snapshotting it here first.
    if instance.pk:
        instance._was_published = (
            Assignment.objects
            .filter(pk=instance.pk)
            .values_list("is_published", flat=True)
            .first()
        ) or False
    else:
        instance._was_published = False


@receiver(post_save, sender=Assignment)
def assignment_created(sender, instance, created, **kwargs):
    # Fires on FIRST PUBLICATION, not on creation. A draft saved now and
    # published tomorrow notifies tomorrow; re-saving an already-published
    # assignment (fixing a typo) notifies nobody a second time.
    was = getattr(instance, "_was_published", False)
    if was or not instance.is_published:
        return

    subject = instance.chapter.subject
    course = subject.course

    enrollments = _enrollments_for(course, instance.batch_id)

    _bulk_notify_students(
        enrollments=enrollments,
        obj=instance,
        activity_type=Activity.TYPE_ASSIGNMENT,
        title=f"New assignment: {instance.title}",
        due_date=instance.due_date,
        subject_id=subject.id,
        subject_name=subject.name,
        verb="assignment.posted",
        # Matches the student bell's ASSIGNMENT branch and the
        # subjects/:subjectId/assignments route.
        link_url=f"/subjects/{subject.id}/assignments",
    )


# =====================================================
# ASSIGNMENT SUBMITTED → notify subject teachers
# =====================================================

@receiver(pre_save, sender=AssignmentSubmission)
def cache_submission_file(sender, instance, **kwargs):
    """Snapshot the stored file so post_save can tell a RE-upload from any
    other save. Same shape as cache_assignment_published_state, and for the
    same reason: post_save only ever sees the row as it now is."""
    if instance.pk:
        instance._previous_file = (
            AssignmentSubmission.objects
            .filter(pk=instance.pk)
            .values_list("submitted_file", flat=True)
            .first()
        )
    else:
        instance._previous_file = None


@receiver(post_save, sender=AssignmentSubmission)
def assignment_submitted(sender, instance, created, **kwargs):
    # RESUBMISSIONS notify too. This used to be a bare `if not created:
    # return`, but SubmitAssignmentView uses update_or_create — so a student
    # who re-uploaded a corrected PDF produced no teacher notification at all,
    # while submitted_at DID move and silently flipped the On-time/Late chip
    # on a submission the teacher may already have graded.
    #
    # Gated on the FILE actually changing, not merely on a save: grading saves
    # the same row (assignments/views.py) and must stay silent here — the
    # student gets the grading notification, the teacher doesn't need one for
    # their own click.
    resubmitted = False
    if not created:
        previous = getattr(instance, "_previous_file", None)
        current = instance.submitted_file.name if instance.submitted_file else None
        if previous == current:
            return
        resubmitted = True

    assignment = instance.assignment
    subject = assignment.chapter.subject
    student_name = _display_name(instance.student)

    # WHICH teachers to tell. This used to filter `batch__isnull=True`, which
    # silently excluded every batch-scoped teacher — the people most likely to
    # own the submission. A teacher assigned only to "Morning 2026" was never
    # told when one of their own students submitted.
    #
    # Now: course-wide teachers (batch IS NULL) plus the teachers of the
    # submitting learner's batch. distinct() because one teacher can hold
    # several teaching assignments on a subject and must not be notified twice.
    learner_batch_id = None
    if instance.learner_profile_id:
        from enrollments.services import active_batch_id
        learner_batch_id = active_batch_id(
            learner_profile=instance.learner_profile,
            course_id=subject.course_id,
        )

    tas = subject.teaching_assignments.filter(is_active=True)
    if learner_batch_id is not None:
        tas = tas.filter(Q(batch__isnull=True) | Q(batch_id=learner_batch_id))
    # learner_batch_id None → the learner isn't placed in a batch, so we
    # cannot tell whose cohort this is; tell every teacher on the subject
    # rather than nobody. Same deliberate over-share as _enrollments_for.

    notified = set()
    for ta in tas.select_related("teacher").distinct():
        if ta.teacher_id in notified:
            continue
        notified.add(ta.teacher_id)
        _notify_teacher(
            teacher=ta.teacher,
            obj=assignment,
            activity_type=Activity.TYPE_SUBMISSION,
            # Named differently on a re-upload: if the teacher already graded
            # the first file, "submitted" would read as a duplicate they can
            # ignore, when in fact the work they marked no longer exists.
            title=(
                f"{student_name} re-submitted: {assignment.title}"
                if resubmitted else
                f"{student_name} submitted: {assignment.title}"
            ),
            due_date=assignment.due_date,
            subject_id=subject.id,
            subject_name=subject.name,
            verb="assignment.submitted",
            # The faculty bell's SUBMISSION branch routes on subject_id + the
            # parent object id; this path is its direct equivalent.
            link_url=f"/teacher/classes/{subject.id}/assignments/{assignment.id}/submissions",
        )


# =====================================================
# QUIZ — cache old published state before save
# (unchanged fix: post_save re-queried the saved row, so the
#  False→True transition was never observed)
# =====================================================

@receiver(pre_save, sender=Quiz)
def cache_quiz_published_state(sender, instance, **kwargs):
    if instance.pk:
        instance._was_published = (
            Quiz.objects
            .filter(pk=instance.pk)
            .values_list("is_published", flat=True)
            .first()
        ) or False
    else:
        instance._was_published = False


@receiver(post_save, sender=Quiz)
def quiz_published(sender, instance, created, **kwargs):
    was = getattr(instance, "_was_published", False)
    if was or not instance.is_published:
        return

    subject = instance.subject
    course = subject.course

    enrollments = _enrollments_for(course, instance.batch_id)

    _bulk_notify_students(
        enrollments=enrollments,
        obj=instance,
        activity_type=Activity.TYPE_QUIZ,
        title=f"Quiz available: {instance.title}",
        due_date=None,  # quizzes have no due date
        subject_id=subject.id,
        subject_name=subject.name,
        verb="quiz.posted",
        # Same path quizzes/views.py already uses for quiz.reminder, so a
        # "posted" and a "reminder" about one quiz land on the same page.
        link_url=f"/subjects/quiz/{subject.id}",
    )


# =====================================================
# QUIZ SUBMITTED → notify quiz author
# =====================================================

@receiver(post_save, sender=QuizAttempt)
def quiz_submitted(sender, instance, created, **kwargs):
    from quizzes.models import QuizAttempt as QA
    if instance.status != QA.STATUS_SUBMITTED or not instance.submitted_at:
        return

    quiz = instance.quiz
    subject = quiz.subject
    student_name = _display_name(instance.student)
    teacher = quiz.created_by
    if not teacher:
        return

    # Dedup — don't double-notify the same attempt
    content_type = ContentType.objects.get_for_model(quiz)
    if Activity.objects.filter(
        user=teacher,
        audience=Activity.AUDIENCE_TEACHER,
        type=Activity.TYPE_SUBMISSION,
        content_type=content_type,
        object_id=quiz.id,
        title__startswith=student_name,
    ).exists():
        return

    _notify_teacher(
        teacher=teacher,
        obj=quiz,
        activity_type=Activity.TYPE_SUBMISSION,
        title=f"{student_name} submitted: {quiz.title}",
        due_date=None,  # quizzes have no due date
        subject_id=subject.id,
        subject_name=subject.name,
        # The teacher bell routes quiz submissions to /quizzes, not the
        # assignment-submissions page — keep the discriminator.
        extra={"subtype": "quiz_submission"},
    )


# =====================================================
# LIVE SESSION CREATED → notify enrolled learner profiles
# =====================================================

@receiver(post_save, sender=LiveSession)
def session_created(sender, instance, created, **kwargs):
    if not created:
        return

    course = instance.course
    subject = instance.subject
    subject_id = subject.id if subject else None
    subject_name = subject.name if subject else ""

    enrollments = (
        Enrollment.objects
        .filter(course=course, status=Enrollment.STATUS_ACTIVE)
        .select_related("user", "learner_profile")
    )

    _bulk_notify_students(
        enrollments=enrollments,
        obj=instance,
        activity_type=Activity.TYPE_SESSION,
        title=f"Live session scheduled: {instance.title}",
        due_date=instance.start_time,
        subject_id=subject_id,
        subject_name=subject_name,
        extra={"start_time": instance.start_time.isoformat()
               if instance.start_time else None},
    )
