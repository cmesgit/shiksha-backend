"""Backfill Subscription rows for existing active Enrollments.

Active enrollments created before the Subscription model existed have no
matching Subscription row, so /courses/my/ shows them as "Legacy access" in
the student dashboard. This command creates one Subscription per active
Enrollment that lacks one, granting a fresh window starting now (default) or
backdated to enrolled_at (--from-enrollment-date).

Idempotent: skips enrollments that already have any Subscription row.

Usage:
    python manage.py backfill_subscriptions --dry-run
    python manage.py backfill_subscriptions
    python manage.py backfill_subscriptions --from-enrollment-date
    python manage.py backfill_subscriptions --days 60
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from enrollments.models import Enrollment, Subscription


class Command(BaseCommand):
    help = "Create Subscription rows for active Enrollments that don't have one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would happen without writing to the DB.",
        )
        parser.add_argument(
            "--from-enrollment-date",
            action="store_true",
            help=(
                "Use enrollment.enrolled_at as starts_at instead of now. "
                "May produce already-expired subscriptions for old enrollments."
            ),
        )
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help=(
                "Override per-course duration with a fixed number of days. "
                "Defaults to course.subscription_duration_days."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        from_enrollment_date = options["from_enrollment_date"]
        days_override = options["days"]
        now = timezone.now()

        active_enrollments = (
            Enrollment.objects
            .filter(status=Enrollment.STATUS_ACTIVE)
            .select_related("user", "course")
            .order_by("enrolled_at")
        )

        existing_pairs = set(
            Subscription.objects.values_list("user_id", "course_id")
        )

        to_create = []
        skipped = 0

        for enr in active_enrollments:
            if (enr.user_id, enr.course_id) in existing_pairs:
                skipped += 1
                continue

            days = days_override or enr.course.subscription_duration_days or 30
            starts_at = enr.enrolled_at if from_enrollment_date else now
            expires_at = starts_at + timedelta(days=days)

            to_create.append(
                Subscription(
                    user=enr.user,
                    course=enr.course,
                    starts_at=starts_at,
                    expires_at=expires_at,
                    status=Subscription.STATUS_ACTIVE,
                )
            )

        self.stdout.write(f"Active enrollments scanned: {active_enrollments.count()}")
        self.stdout.write(f"Already have a subscription (skipped): {skipped}")
        self.stdout.write(f"To create: {len(to_create)}")

        if dry_run:
            for sub in to_create[:10]:
                self.stdout.write(
                    f"  [dry-run] {sub.user.email} → {sub.course.title}: "
                    f"{sub.starts_at:%Y-%m-%d} → {sub.expires_at:%Y-%m-%d}"
                )
            if len(to_create) > 10:
                self.stdout.write(f"  ... and {len(to_create) - 10} more")
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
            return

        if not to_create:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
            return

        with transaction.atomic():
            Subscription.objects.bulk_create(to_create)

        self.stdout.write(self.style.SUCCESS(f"Created {len(to_create)} subscriptions."))
