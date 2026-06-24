"""
enrollments/management/commands/backfill_profile_links.py

Fills learner_profile on existing Subscription and Enrollment rows that were
created before the per-profile model (they have learner_profile = NULL).

POLICY (matches the decision for this migration):
  • If the row's account has exactly ONE active learner profile → assign it.
  • If the account has a single SELF profile among several → assign the SELF one.
  • If the account has MULTIPLE non-obvious profiles → REPORT, don't guess.
  • If the account has NO learner profile (e.g. a teacher-only account that
    somehow holds a subscription) → REPORT and SKIP. These are the invalid
    rows; review the report, then delete them with the one-liner printed at
    the end.

Always run --dry-run first.

    python manage.py backfill_profile_links --dry-run
    python manage.py backfill_profile_links
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from enrollments.models import Subscription, Enrollment
from accounts.models import LearnerProfile


def _resolve_profile(account):
    """Best-effort single learner profile for an account, or None if ambiguous."""
    qs = LearnerProfile.objects.filter(account=account, is_active=True)
    n = qs.count()
    if n == 0:
        return None, "no_profile"
    if n == 1:
        return qs.first(), "single"
    selfs = qs.filter(relationship="SELF")
    if selfs.count() == 1:
        return selfs.first(), "self"
    return None, "ambiguous"


class Command(BaseCommand):
    help = "Backfill learner_profile on legacy Subscription/Enrollment rows."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        assigned = {"single": 0, "self": 0}
        orphans = []      # no learner profile at all
        ambiguous = []    # multiple profiles, can't choose

        def process(Model, label):
            nonlocal assigned, orphans, ambiguous
            rows = Model.objects.filter(learner_profile__isnull=True).select_related("user")
            self.stdout.write(f"\n{label}: {rows.count()} row(s) with NULL learner_profile")
            for row in rows:
                profile, reason = _resolve_profile(row.user)
                if profile is None and reason == "no_profile":
                    orphans.append((label, row.id, row.user.email))
                    continue
                if profile is None and reason == "ambiguous":
                    ambiguous.append((label, row.id, row.user.email))
                    continue
                assigned[reason] += 1
                if dry:
                    self.stdout.write(f"  [dry] {label} {row.id} {row.user.email} -> profile {profile.id} ({reason})")
                else:
                    row.learner_profile = profile
                    row.save(update_fields=["learner_profile"])

        with transaction.atomic():
            process(Subscription, "Subscription")
            process(Enrollment, "Enrollment")
            if dry:
                transaction.set_rollback(True)

        self.stdout.write("\n──────── SUMMARY ────────")
        self.stdout.write(f"Assigned (single profile): {assigned['single']}")
        self.stdout.write(f"Assigned (SELF profile):   {assigned['self']}")
        self.stdout.write(f"AMBIGUOUS (skipped, multiple profiles): {len(ambiguous)}")
        for lbl, rid, email in ambiguous:
            self.stdout.write(f"   - {lbl} {rid} {email}")
        self.stdout.write(f"ORPHANS (skipped, account has NO learner profile): {len(orphans)}")
        for lbl, rid, email in orphans:
            self.stdout.write(f"   - {lbl} {rid} {email}")

        if orphans:
            self.stdout.write(
                "\nThese ORPHAN rows are invalid (an account with no learner "
                "profile cannot hold academy access). After reviewing the list "
                "above, delete them with:\n"
                "  Subscription.objects.filter(learner_profile__isnull=True, "
                "user__learner_profiles__isnull=True).distinct().delete()\n"
                "  Enrollment.objects.filter(learner_profile__isnull=True, "
                "user__learner_profiles__isnull=True).distinct().delete()"
            )
        if dry:
            self.stdout.write("\n(dry-run — nothing was written)")
