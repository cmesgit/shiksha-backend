# Test-only fixture builders for the chat app's functional test suite
# (M0/M1/M2 regression + M3). Not part of the shipped application.
#
# Deliberately plain functions rather than pulling in factory_boy — this
# codebase has no existing factory-library dependency, and every fixture
# shape needed here (a user, a learner, a teacher, a course a learner is
# actively subscribed to, ...) is small enough that a real dependency
# would cost more than it saves.
#
# IMPORTANT: LearnerProfile/TeacherProfile creation below goes through the
# real model .objects.create(), which means accounts/signals.py's
# post_save receivers run for real and create the matching accounts
# .Identity row — exactly as they do in production. Tests that need an
# Identity row (M1 regression) don't need to construct one by hand.
import uuid
from datetime import timedelta

from django.utils import timezone

from accounts.models import User, LearnerProfile, TeacherProfile
from courses.models import Course, Subject, TeachingAssignment
from enrollments.models import Subscription


def make_user(username=None, email=None):
    tag = uuid.uuid4().hex[:10]
    username = username or f"user_{tag}"
    email = email or f"{username}@example.test"
    return User.objects.create(username=username, email=email)


def make_learner(account=None, display_name="Test Learner",
                  relationship=LearnerProfile.RELATIONSHIP_SELF):
    account = account or make_user()
    return LearnerProfile.objects.create(
        account=account, display_name=display_name, relationship=relationship,
    )


def make_teacher(user=None):
    user = user or make_user()
    return TeacherProfile.objects.create(user=user)


def make_course(title=None):
    title = title or f"Course {uuid.uuid4().hex[:6]}"
    return Course.objects.create(title=title)


def make_subject(course=None, name="Physics"):
    course = course or make_course()
    return Subject.objects.create(course=course, name=name)


def assign_teacher_to_subject(subject, teacher_profile):
    """Makes `teacher_profile` teach `subject` — teacher_in_course()'s
    exact query is TeachingAssignment.objects.filter(subject__course_id=...,
    teacher=tp.user, is_active=True), so this is the minimal real fixture
    for that."""
    return TeachingAssignment.objects.create(
        subject=subject, teacher=teacher_profile.user, batch=None, is_active=True,
    )


def make_active_subscription(learner_profile, course):
    """Gives `learner_profile` LIVE access to `course` — the exact
    condition learner_in_course()/has_active_subscription() check
    (status=ACTIVE, expires_at in the future), scoped to this
    learner_profile specifically (has_active_subscription's strict mode)."""
    now = timezone.now()
    return Subscription.objects.create(
        user=learner_profile.account,
        learner_profile=learner_profile,
        course=course,
        starts_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=29),
        status=Subscription.STATUS_ACTIVE,
    )


def make_expired_subscription(learner_profile, course):
    now = timezone.now()
    return Subscription.objects.create(
        user=learner_profile.account,
        learner_profile=learner_profile,
        course=course,
        starts_at=now - timedelta(days=60),
        expires_at=now - timedelta(days=30),
        status=Subscription.STATUS_EXPIRED,
    )


def enrolled_learner_and_teacher(course=None):
    """Full realistic setup for the L<->T 'shared active course' DM rule:
    one course, a subject, a teacher assigned to it, and a learner with a
    live subscription to the course. Returns (learner_profile,
    teacher_profile, course)."""
    course = course or make_course()
    subject = make_subject(course=course)
    teacher = make_teacher()
    assign_teacher_to_subject(subject, teacher)
    learner = make_learner()
    make_active_subscription(learner, course)
    return learner, teacher, course


def make_direct_conversation():
    """A DIRECT conversation between a fresh learner and a fresh teacher.
    Returns (conversation, learner_participant, teacher_participant)."""
    from chat import services
    from chat.models import Participant

    learner = make_learner()
    teacher = make_teacher()
    conv = services.ensure_direct(
        Participant.KIND_LEARNER, learner, Participant.KIND_TEACHER, teacher,
    )
    learner_p = services.participant_for(conv, Participant.KIND_LEARNER, learner)
    teacher_p = services.participant_for(conv, Participant.KIND_TEACHER, teacher)
    return conv, learner_p, teacher_p
