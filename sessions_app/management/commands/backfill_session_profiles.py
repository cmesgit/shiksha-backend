"""
sessions_app/management/commands/backfill_session_profiles.py

Populates learner_profile on legacy PrivateSession rows that predate
per-profile attribution (learner_profile = NULL).

Unlike quiz attempts / assignment submissions, a private session has no course
FK to disambiguate which child booked it, so attribution is:
  1. account.default_learner_profile() exists → assign the default (SELF)
     profile. Before multi-profile existed every account had exactly one
     profile, so for legacy rows this is who actually booked.
  2. Account has NO learner profile → REPORT + SKIP.

Always dry-run first:
    python manage.py backfill_session_profiles --dry-run
    python manage.py backfill_session_profiles
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from sessions_app.models import PrivateSession


class Command(BaseCommand):
    help = "Backfill learner_profile on legacy private sessions."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        assigned = 0
        orphans = []

        rows = (
            PrivateSession.objects
            .filter(learner_profile__isnull=True)
            .select_related("requested_by")
        )
        total = rows.count()
        self.stdout.write(
            f"PrivateSession: {total} row(s) with NULL learner_profile"
        )

        with transaction.atomic():
            for row in rows.iterator():
                profile = row.requested_by.default_learner_profile()
                if profile is None:
                    orphans.append((str(row.id), row.requested_by.email))
                    continue
                assigned += 1
                if dry:
                    self.stdout.write(
                        f"  [dry] {row.id} {row.requested_by.email} -> "
                        f"{profile.id} ({profile.display_name})"
                    )
                else:
                    row.learner_profile = profile
                    row.save(update_fields=["learner_profile"])
            if dry:
                transaction.set_rollback(True)

        self.stdout.write("\n──────── SUMMARY ────────")
        self.stdout.write(f"Assigned: {assigned}")
        self.stdout.write(
            f"ORPHANS (account has NO learner profile — skipped): {len(orphans)}"
        )
        for rid, email in orphans:
            self.stdout.write(f"   - {rid} {email}")
        if dry:
            self.stdout.write("\n(dry-run — nothing written)")
