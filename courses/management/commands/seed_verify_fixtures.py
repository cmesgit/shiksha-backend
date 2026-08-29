"""Extra fixtures the verification harnesses in `scripts/` need.

`seed_demo_data` builds one course with ONE batch and one student. That is
enough to click through the product, but not enough to PROVE batch isolation:
you cannot show that Batch A's material is hidden from Batch B without a
Batch B and someone in it.

This command adds, idempotently, on top of `seed_demo_data`:

  * a staff account          — the demo seed makes none, so every admin-side
                               check was unrunnable
  * a second batch           — DEMO-B1
  * a second student in it   — demo.student.b@shiksha.test
  * an ACTIVE SUBSCRIPTION for that student

THE SUBSCRIPTION IS THE POINT, and it is why this is a command rather than a
paragraph in a docstring. A student who is merely ENROLLED is rejected by the
entitlement gate (402 / empty list) *before* any batch check runs — so a
batch-isolation assertion against them passes without ever exercising the
batch rule. That is exactly how a first verification run "confirmed" isolation
for materials and quizzes while a real cross-batch leak sat in the quiz list.

Also creates a COMPLETED live session that HAS a batch, so the
recording-from-a-live-session batch-override case has something to run against.

    python manage.py seed_demo_data      --settings=config.settings_test
    python manage.py seed_verify_fixtures --settings=config.settings_test
"""
import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

DEMO_PASSWORD = "ShikshaDemo@2026"
ADMIN_EMAIL = "verify.admin@shiksha.test"
STUDENT_B_EMAIL = "demo.student.b@shiksha.test"


class Command(BaseCommand):
    help = "Admin + second batch + entitled second student for scripts/verify_*.py"

    @transaction.atomic
    def handle(self, *args, **options):
        from accounts.models import LearnerProfile, Role, User, UserRole
        from courses.models import Batch, Course, Subject
        from enrollments.models import Enrollment, Subscription
        from livestream.models import LiveSession

        course = Course.objects.filter(
            title__startswith="Class 11 Science"
        ).first()
        if course is None:
            self.stderr.write(self.style.ERROR(
                "No demo course found — run seed_demo_data first."))
            return

        # ── staff account ────────────────────────────────────────────────
        admin, _ = User.objects.get_or_create(
            email=ADMIN_EMAIL, defaults={"username": "verify.admin"},
        )
        admin.is_staff = True
        admin.is_superuser = True
        admin.is_verified = True
        admin.set_password(DEMO_PASSWORD)
        admin.save()
        self.stdout.write(f"admin           {ADMIN_EMAIL}")

        # ── second batch ─────────────────────────────────────────────────
        batch_b, _ = Batch.objects.get_or_create(
            course=course, code="DEMO-B1",
            defaults={"name": "Demo Batch B1"},
        )
        self.stdout.write(f"batch           {batch_b.name}")

        # ── second student, in batch B ───────────────────────────────────
        student_role, _ = Role.objects.get_or_create(name="STUDENT")
        student_b, _ = User.objects.get_or_create(
            email=STUDENT_B_EMAIL, defaults={"username": "demo.student.b"},
        )
        student_b.is_verified = True
        student_b.set_password(DEMO_PASSWORD)
        student_b.save()
        UserRole.objects.get_or_create(user=student_b, role=student_role)

        profile_b, _ = LearnerProfile.objects.get_or_create(
            account=student_b,
            defaults={"display_name": "Bhavna", "full_name": "Bhavna",
                      "is_default": True},
        )
        Enrollment.objects.update_or_create(
            user=student_b, learner_profile=profile_b, course=course,
            defaults={"batch": batch_b, "status": Enrollment.STATUS_ACTIVE},
        )

        # THE SUBSCRIPTION. See this module's docstring — without it every
        # batch assertion against this student passes for the wrong reason.
        # `starts_at` is NOT NULL on this model.
        now = timezone.now()
        Subscription.objects.update_or_create(
            user=student_b, course=course,
            defaults={
                "status": Subscription.STATUS_ACTIVE,
                "starts_at": now,
                "expires_at": now + timedelta(days=365),
                **({"learner_profile": profile_b}
                   if hasattr(Subscription, "learner_profile") else {}),
            },
        )
        self.stdout.write(
            f"student         {STUDENT_B_EMAIL} "
            f"(batch {batch_b.code}, enrolled + SUBSCRIBED)")

        # ── a completed live session that HAS a batch ────────────────────
        subject = Subject.objects.filter(course=course).first()
        teacher = User.objects.filter(
            email="demo.faculty@shiksha.test").first()
        batch_a = (Batch.objects.filter(course=course, code="DEMO-A1").first()
                   or Batch.objects.filter(course=course).first())
        if subject and teacher and batch_a and not LiveSession.objects.filter(
            course=course, batch=batch_a,
            status=LiveSession.STATUS_COMPLETED,
        ).exists():
            LiveSession.objects.create(
                course=course, subject=subject, batch=batch_a,
                title="Batched demo class",
                room_name=f"session_{uuid.uuid4().hex}",
                start_time=now - timedelta(days=1),
                end_time=now - timedelta(days=1) + timedelta(hours=1),
                status=LiveSession.STATUS_COMPLETED, created_by=teacher,
            )
            self.stdout.write("live session    batched, COMPLETED")

        self.stdout.write(self.style.SUCCESS(
            f"Verification fixtures ready. Password: {DEMO_PASSWORD}"))
