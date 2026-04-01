from django.db.models.signals import post_save
from django.dispatch import receiver

from assignments.models import Assignment
from quizzes.models import Quiz
from livestream.models import LiveSession
from enrollments.models import Enrollment

from .services import create_activity
from .models import Activity


# =========================
# ASSIGNMENT CREATED
# =========================
@receiver(post_save, sender=Assignment)
def assignment_created(sender, instance, created, **kwargs):

    if not created:
        return

    course = instance.chapter.subject.course

    students = Enrollment.objects.filter(
        course=course,
        status=Enrollment.STATUS_ACTIVE
    ).select_related("user")

    # 🔥 notify students
    for enrollment in students:
        create_activity(
            user=enrollment.user,
            obj=instance,
            type=Activity.TYPE_ASSIGNMENT,
            title=f"New assignment: {instance.title}",
            due_date=instance.due_date
        )

    # 🔥 notify teacher (FIXED)
    user = getattr(instance, "created_by", None)
    if user:
        create_activity(
            user=user,
            obj=instance,
            type=Activity.TYPE_ASSIGNMENT,
            title=f"You created: {instance.title}",
            due_date=instance.due_date
        )


# =========================
# QUIZ PUBLISHED
# =========================
@receiver(post_save, sender=Quiz)
def quiz_published(sender, instance, created, **kwargs):

    if not instance.is_published:
        return

    course = instance.subject.course

    students = Enrollment.objects.filter(
        course=course,
        status=Enrollment.STATUS_ACTIVE
    ).select_related("user")

    # 🔥 notify students
    for enrollment in students:
        create_activity(
            user=enrollment.user,
            obj=instance,
            type=Activity.TYPE_QUIZ,
            title=f"Quiz available: {instance.title}",
            due_date=instance.due_date
        )

    # 🔥 notify teacher (FIXED)
    user = getattr(instance, "created_by", None)
    if user:
        create_activity(
            user=user,
            obj=instance,
            type=Activity.TYPE_QUIZ,
            title=f"You published quiz: {instance.title}",
            due_date=instance.due_date
        )


# =========================
# LIVE SESSION CREATED
# =========================
@receiver(post_save, sender=LiveSession)
def session_created(sender, instance, created, **kwargs):

    if not created:
        return

    course = instance.course

    students = Enrollment.objects.filter(
        course=course,
        status=Enrollment.STATUS_ACTIVE
    ).select_related("user")

    # 🔥 notify students
    for enrollment in students:
        create_activity(
            user=enrollment.user,
            obj=instance,
            type=Activity.TYPE_SESSION,
            title=f"Live session scheduled: {instance.title}",
            due_date=instance.start_time
        )

    # 🔥 notify teacher (FIXED)
    user = getattr(instance, "created_by", None)
    if user:
        create_activity(
            user=user,
            obj=instance,
            type=Activity.TYPE_SESSION,
            title=f"You scheduled session: {instance.title}",
            due_date=instance.start_time
        )
        