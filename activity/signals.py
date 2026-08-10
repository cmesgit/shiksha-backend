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


def _bulk_notify_students(enrollments, obj, activity_type, title, due_date,
                          subject_id, subject_name, extra=None):
    """One Activity per (account, learner_profile) enrollment row, then a
    targeted WS push per row. UUID pks are generated client-side, so the
    objects passed to bulk_create already have ids we can serialize."""
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

    for act in activities:
        # bulk_create skips auto_now_add on some backends' returned attrs;
        # stamp a value so the WS frame always has created_at.
        if act.created_at is None:
            act.created_at = now
        push_ws_notification(act.user_id, _ws_payload(act, extra))


def _notify_teacher(teacher, obj, activity_type, title, due_date,
                    subject_id, subject_name, extra=None):
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
    push_ws_notification(teacher.id, _ws_payload(act, extra))


# =====================================================
# ASSIGNMENT CREATED → notify enrolled learner profiles
# =====================================================

@receiver(post_save, sender=Assignment)
def assignment_created(sender, instance, created, **kwargs):
    if not created:
        return

    subject = instance.chapter.subject
    course = subject.course

    enrollments = (
        Enrollment.objects
        .filter(course=course, status=Enrollment.STATUS_ACTIVE)
        .select_related("user", "learner_profile")
    )

    _bulk_notify_students(
        enrollments=enrollments,
        obj=instance,
        activity_type=Activity.TYPE_ASSIGNMENT,
        title=f"New assignment: {instance.title}",
        due_date=instance.due_date,
        subject_id=subject.id,
        subject_name=subject.name,
    )


# =====================================================
# ASSIGNMENT SUBMITTED → notify subject teachers
# =====================================================

@receiver(post_save, sender=AssignmentSubmission)
def assignment_submitted(sender, instance, created, **kwargs):
    if not created:
        return

    assignment = instance.assignment
    subject = assignment.chapter.subject
    student_name = _display_name(instance.student)

    for st in subject.subject_teachers.select_related("teacher").all():
        _notify_teacher(
            teacher=st.teacher,
            obj=assignment,
            activity_type=Activity.TYPE_SUBMISSION,
            title=f"{student_name} submitted: {assignment.title}",
            due_date=assignment.due_date,
            subject_id=subject.id,
            subject_name=subject.name,
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

    enrollments = (
        Enrollment.objects
        .filter(course=course, status=Enrollment.STATUS_ACTIVE)
        .select_related("user", "learner_profile")
    )

    _bulk_notify_students(
        enrollments=enrollments,
        obj=instance,
        activity_type=Activity.TYPE_QUIZ,
        title=f"Quiz available: {instance.title}",
        due_date=None,  # quizzes have no due date
        subject_id=subject.id,
        subject_name=subject.name,
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
