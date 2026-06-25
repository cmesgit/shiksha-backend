"""
Seed dummy accounts for functional testing.

Creates 2 student + 2 teacher accounts, all wired to ONE shared course:
  - both teachers are FACULTY, approved on the academy track, and attached
    to the course's subject (SubjectTeacher)
  - both students get a default LearnerProfile, enrolled in the course with
    an active subscription

Reflects the post-refactor schema:
  - the old one-to-one Profile is gone; learner identity lives on
    LearnerProfile (one account -> many learners)
  - teacher approval is driven by per-track status (academy/skill), synced
    into is_approved/teacher_type via TeacherProfile.sync_type_from_tracks()
  - Enrollment / Subscription key on learner_profile

Idempotent: safe to run repeatedly.

    python manage.py seed_dummy_accounts
    python manage.py seed_dummy_accounts --password Test@12345
    python manage.py seed_dummy_accounts --wipe   # delete the dummies first
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import LearnerProfile, Role, TeacherProfile, User, UserRole
from courses.models import Course, Subject, SubjectTeacher
from enrollments.models import Enrollment, Subscription


# --- Fixture definitions ----------------------------------------------------

COURSE_TITLE = "Demo Course (QA)"
SUBJECT_NAME = "Demo Subject"

TEACHERS = [
    {"email": "teacher1@example.com", "username": "qa_teacher1", "first": "Tina", "last": "Teacher"},
    {"email": "teacher2@example.com", "username": "qa_teacher2", "first": "Tom", "last": "Teacher"},
]

STUDENTS = [
    {"email": "student1@example.com", "username": "qa_student1", "first": "Sam", "last": "Student"},
    {"email": "student2@example.com", "username": "qa_student2", "first": "Sara", "last": "Student"},
]

ALL_EMAILS = [u["email"] for u in TEACHERS + STUDENTS]
DEFAULT_PASSWORD = "Test@1234"


class Command(BaseCommand):
    help = "Create 2 student + 2 teacher dummy accounts sharing one course (for testing)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=DEFAULT_PASSWORD,
            help=f"Password set on every dummy account (default: {DEFAULT_PASSWORD}).",
        )
        parser.add_argument(
            "--wipe",
            action="store_true",
            help="Delete the dummy users (and their related rows) before seeding.",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        password = opts["password"]

        if opts["wipe"]:
            deleted, _ = User.objects.filter(email__in=ALL_EMAILS).delete()
            self.stdout.write(self.style.WARNING(f"Wiped dummy users ({deleted} rows)."))

        # 1) Roles
        student_role, _ = Role.objects.get_or_create(name=Role.STUDENT)
        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)

        # 2) Shared course + subject
        course, created = Course.objects.get_or_create(
            title=COURSE_TITLE,
            stream=None,
            board=None,
            defaults={
                "description": "Auto-generated course for QA / functionality testing.",
                "price": 0,
                "subscription_duration_days": 365,
            },
        )
        self._report("course", course.title, created)

        subject, created = Subject.objects.get_or_create(
            course=course,
            name=SUBJECT_NAME,
        )
        self._report("subject", subject.name, created)

        # 3) Teachers
        for spec in TEACHERS:
            user = self._upsert_user(spec, password)
            self._assign_role(user, teacher_role)
            self._approve_teacher(user)

            link, created = SubjectTeacher.objects.get_or_create(
                subject=subject,
                teacher=user,
                defaults={"display_role": SubjectTeacher.ROLE_PRIMARY},
            )
            self._report("teacher", user.email, created, extra="-> subject")

        # 4) Students
        for spec in STUDENTS:
            user = self._upsert_user(spec, password)
            self._assign_role(user, student_role)
            learner = self._ensure_learner_profile(user, spec)

            enrollment, created = Enrollment.objects.get_or_create(
                learner_profile=learner,
                course=course,
                defaults={"user": user, "status": Enrollment.STATUS_ACTIVE},
            )
            self._report("student", user.email, created, extra="-> enrolled")

            self._ensure_active_subscription(user, learner, course)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Done. Dummy accounts ready."))
        self.stdout.write(f"  Course : {course.title}")
        self.stdout.write(f"  Login  : any email below / password '{password}'")
        for email in ALL_EMAILS:
            self.stdout.write(f"           {email}")

    # --- helpers ------------------------------------------------------------

    def _upsert_user(self, spec, password):
        user, _ = User.objects.get_or_create(
            email=spec["email"],
            defaults={"username": spec["username"]},
        )
        user.username = spec["username"]
        user.is_verified = True
        if user.verified_at is None:
            user.verified_at = timezone.now()
        user.set_password(password)
        user.save()
        return user

    def _assign_role(self, user, role):
        """Give the user this role as their single primary role.

        UserRole.clean() now allows several active roles but only one
        primary, so demote any other primary first to stay idempotent.
        """
        UserRole.objects.filter(user=user, is_primary=True).exclude(role=role).update(
            is_primary=False
        )
        ur, _ = UserRole.objects.get_or_create(
            user=user,
            role=role,
            defaults={"is_active": True, "is_primary": True},
        )
        if not ur.is_active or not ur.is_primary:
            ur.is_active = True
            ur.is_primary = True
            ur.save()

    def _approve_teacher(self, user):
        """Create an approved FACULTY teacher profile.

        is_approved / teacher_type are derived fields — drive them through the
        academy track status + sync_type_from_tracks() rather than setting
        is_approved directly.
        """
        tp, _ = TeacherProfile.objects.get_or_create(user=user)
        tp.academy_status = TeacherProfile.TRACK_APPROVED
        tp.sync_type_from_tracks()  # sets teacher_type=FACULTY, is_approved=True
        tp.save()

    def _ensure_learner_profile(self, user, spec):
        learner = user.default_learner_profile()
        if learner is None:
            learner = LearnerProfile.objects.create(
                account=user,
                display_name=f"{spec['first']} {spec['last']}".strip(),
                relationship=LearnerProfile.RELATIONSHIP_SELF,
                is_default=True,
                is_active=True,
                first_name=spec["first"],
                last_name=spec["last"],
            )
        return learner

    def _ensure_active_subscription(self, user, learner, course):
        now = timezone.now()
        active = Subscription.objects.filter(
            learner_profile=learner,
            course=course,
            status=Subscription.STATUS_ACTIVE,
            expires_at__gt=now,
        ).exists()
        if active:
            return
        Subscription.objects.create(
            user=user,
            learner_profile=learner,
            course=course,
            starts_at=now,
            expires_at=now + timedelta(days=course.subscription_duration_days),
            status=Subscription.STATUS_ACTIVE,
        )

    def _report(self, kind, name, created, extra=""):
        verb = "created" if created else "exists "
        style = self.style.SUCCESS if created else self.style.NOTICE
        self.stdout.write(style(f"  [{verb}] {kind}: {name} {extra}".rstrip()))
