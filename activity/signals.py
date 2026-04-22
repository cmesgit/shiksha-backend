# ============================================================
# BACKEND — activity/signals.py  (FULL REPLACEMENT)
# ============================================================

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.contrib.contenttypes.models import ContentType

from assignments.models import Assignment, AssignmentSubmission
from quizzes.models import Quiz, QuizAttempt
from livestream.models import LiveSession
from enrollments.models import Enrollment

from .models import Activity
from livestream.services.notifications import push_ws_notification


# =====================================================
# HELPERS
# =====================================================

def _bulk_notify_students(students, obj, activity_type, title, due_date,
                          subject_id, subject_name, ws_payload):
    content_type = ContentType.objects.get_for_model(obj)
    student_list = list(students)   # evaluate once — avoids double DB hit

    Activity.objects.bulk_create([
        Activity(
            user=enrollment.user,
            type=activity_type,
            title=title,
            due_date=due_date,
            subject_id=subject_id,
            subject_name=subject_name,
            content_type=content_type,
            object_id=obj.id,
        )
        for enrollment in student_list
    ])

    for enrollment in student_list:
        push_ws_notification(enrollment.user.id, ws_payload)


def _notify_teacher(teacher, obj, activity_type, title, due_date,
                    subject_id, subject_name, ws_payload):
    content_type = ContentType.objects.get_for_model(obj)

    Activity.objects.create(
        user=teacher,
        type=activity_type,
        title=title,
        due_date=due_date,
        subject_id=subject_id,
        subject_name=subject_name,
        content_type=content_type,
        object_id=obj.id,
    )

    push_ws_notification(teacher.id, ws_payload)


# =====================================================
# ASSIGNMENT CREATED → notify students
# =====================================================

@receiver(post_save, sender=Assignment)
def assignment_created(sender, instance, created, **kwargs):
    if not created:
        return

    subject = instance.chapter.subject
    course = subject.course

    students = Enrollment.objects.filter(
        course=course,
        status=Enrollment.STATUS_ACTIVE
    ).select_related("user")

    _bulk_notify_students(
        students=students,
        obj=instance,
        activity_type=Activity.TYPE_ASSIGNMENT,
        title=f"New assignment: {instance.title}",
        due_date=instance.due_date,
        subject_id=subject.id,
        subject_name=subject.name,
        ws_payload={
            "type": "assignment",
            "title": f"New assignment: {instance.title}",
            "subject_name": subject.name,
            "due_date": str(instance.due_date) if instance.due_date else None,
            "id": str(instance.id),
            "subject_id": str(subject.id),
        }
    )


# =====================================================
# ASSIGNMENT SUBMITTED → notify teacher
# =====================================================

@receiver(post_save, sender=AssignmentSubmission)
def assignment_submitted(sender, instance, created, **kwargs):
    if not created:
        return

    assignment = instance.assignment
    subject = assignment.chapter.subject
    student = instance.student
    student_name = getattr(getattr(student, "profile", None),
                           "full_name", None) or student.email

    for st in subject.subject_teachers.select_related("teacher").all():
        _notify_teacher(
            teacher=st.teacher,
            obj=assignment,
            activity_type=Activity.TYPE_SUBMISSION,
            title=f"{student_name} submitted: {assignment.title}",
            due_date=assignment.due_date,
            subject_id=subject.id,
            subject_name=subject.name,
            ws_payload={
                "type": "submission",
                "title": f"{student_name} submitted: {assignment.title}",
                "subject_name": subject.name,
                "subject_id": str(subject.id),
                "id": str(assignment.id),
            }
        )


# =====================================================
# QUIZ — cache old published state before save
# FIX: post_save was re-querying the already-saved row,
# so old.is_published was always True and the signal
# never fired. pre_save caches the real previous value.
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
    # Only fire when is_published flips False → True
    was = getattr(instance, "_was_published", False)
    if was or not instance.is_published:
        return

    subject = instance.subject
    course = subject.course

    students = Enrollment.objects.filter(
        course=course,
        status=Enrollment.STATUS_ACTIVE
    ).select_related("user")

    _bulk_notify_students(
        students=students,
        obj=instance,
        activity_type=Activity.TYPE_QUIZ,
        title=f"Quiz available: {instance.title}",
        due_date=instance.due_date,
        subject_id=subject.id,
        subject_name=subject.name,
        ws_payload={
            "type": "quiz",
            "title": f"Quiz available: {instance.title}",
            "subject_name": subject.name,
            "due_date": str(instance.due_date) if instance.due_date else None,
            "id": str(instance.id),
            "subject_id": str(subject.id),
        }
    )


# =====================================================
# QUIZ SUBMITTED → notify teacher
# =====================================================

@receiver(post_save, sender=QuizAttempt)
def quiz_submitted(sender, instance, created, **kwargs):
    from quizzes.models import QuizAttempt as QA
    if instance.status != QA.STATUS_SUBMITTED or not instance.submitted_at:
        return

    quiz = instance.quiz
    subject = quiz.subject
    student = instance.student
    student_name = getattr(getattr(student, "profile", None),
                           "full_name", None) or student.email
    teacher = quiz.created_by
    if not teacher:
        return

    # Dedup — don't double-notify
    content_type = ContentType.objects.get_for_model(quiz)
    if Activity.objects.filter(
        user=teacher,
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
        due_date=quiz.due_date,
        subject_id=subject.id,
        subject_name=subject.name,
        ws_payload={
            "type": "submission",
            "title": f"{student_name} submitted: {quiz.title}",
            "subject_name": subject.name,
            "subject_id": str(subject.id),
            "id": str(quiz.id),
        }
    )


# =====================================================
# LIVE SESSION CREATED → notify students
# =====================================================

@receiver(post_save, sender=LiveSession)
def session_created(sender, instance, created, **kwargs):
    if not created:
        return

    course = instance.course
    subject = instance.subject
    subject_id = subject.id if subject else None
    subject_name = subject.name if subject else ""

    students = Enrollment.objects.filter(
        course=course,
        status=Enrollment.STATUS_ACTIVE
    ).select_related("user")

    _bulk_notify_students(
        students=students,
        obj=instance,
        activity_type=Activity.TYPE_SESSION,
        title=f"Live session scheduled: {instance.title}",
        due_date=instance.start_time,
        subject_id=subject_id,
        subject_name=subject_name,
        ws_payload={
            "type": "live_session",
            "title": f"Live session scheduled: {instance.title}",
            "subject_name": subject_name,
            "start_time": instance.start_time.isoformat(),
            "id": str(instance.id),
            "subject_id": str(subject_id) if subject_id else None,
        }
    )
