"""
enrollments/management/commands/backfill_profile_links.py

Populates learner_profile on legacy Subscription / Enrollment / EnrollmentRequest
rows that predate the per-profile model (learner_profile = NULL).

Policy:
  • account.default_learner_profile() exists  → assign it.
  • account has NO learner profile (e.g. a teacher-only account that somehow
    holds a subscription)                      → REPORT + SKIP (invalid row).

Always dry-run first:
    python manage.py backfill_profile_links --dry-run
    python manage.py backfill_profile_links
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from enrollments.models import Subscription, Enrollment, EnrollmentRequest


class Command(BaseCommand):
    help = "Backfill learner_profile on legacy enrollment/subscription rows."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        assigned = 0
        orphans = []

        def process(Model, label):
            nonlocal assigned, orphans
            rows = Model.objects.filter(learner_profile__isnull=True).select_related("user")
            self.stdout.write(f"\n{label}: {rows.count()} row(s) with NULL learner_profile")
            for row in rows:
                profile = row.user.default_learner_profile()
                if profile is None:
                    orphans.append((label, str(row.id), row.user.email))
                    continue
                assigned += 1
                if dry:
                    self.stdout.write(f"  [dry] {label} {row.id} {row.user.email} -> {profile.id}")
                else:
                    row.learner_profile = profile
                    row.save(update_fields=["learner_profile"])

        with transaction.atomic():
            process(Subscription, "Subscription")
            process(Enrollment, "Enrollment")
            process(EnrollmentRequest, "EnrollmentRequest")
            if dry:
                transaction.set_rollback(True)

        self.stdout.write("\n──────── SUMMARY ────────")
        self.stdout.write(f"Assigned: {assigned}")
        self.stdout.write(f"ORPHANS (account has NO learner profile — skipped): {len(orphans)}")
        for lbl, rid, email in orphans:
            self.stdout.write(f"   - {lbl} {rid} {email}")
        if orphans:
            self.stdout.write(
                "\nThese rows are invalid (no learner profile can own academy access). "
                "After reviewing, delete with:\n"
                "  Subscription.objects.filter(learner_profile__isnull=True).delete()\n"
                "  Enrollment.objects.filter(learner_profile__isnull=True).delete()\n"
                "  EnrollmentRequest.objects.filter(learner_profile__isnull=True).delete()"
            )
        if dry:
            self.stdout.write("\n(dry-run — nothing written)")
