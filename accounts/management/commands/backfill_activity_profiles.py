"""
accounts/management/commands/backfill_activity_profiles.py

Populates learner_profile on legacy QuizAttempt / AssignmentSubmission rows
that predate the per-profile model (learner_profile = NULL). Companion to
enrollments' backfill_profile_links — run that one FIRST so enrollments carry
profile links this command can use for attribution.

Attribution policy, per row (most specific wins):
  1. Exactly ONE of the account's learner profiles holds an Enrollment for
     the row's course                     → assign that profile.
  2. Otherwise, account.default_learner_profile() exists
                                          → assign the default (SELF) profile.
     (Before multi-profile existed every account had exactly one profile, so
     for legacy rows this is the profile that actually did the work.)
  3. Account has NO learner profile      → REPORT + SKIP (invalid row).

Rule 1 matters for accounts that added children BEFORE this migration: if
only the child is enrolled in the course, the attempt was the child's.
Ambiguous rows (several profiles enrolled in the same course) fall back to
the default profile and are listed in the summary for manual review.

Always dry-run first:
    python manage.py backfill_activity_profiles --dry-run
    python manage.py backfill_activity_profiles
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from assignments.models import AssignmentSubmission
from enrollments.models import Enrollment
from quizzes.models import QuizAttempt


class Command(BaseCommand):
    help = "Backfill learner_profile on legacy quiz attempts and assignment submissions."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        assigned = 0
        ambiguous = []
        orphans = []

        def resolve(user, course_id):
            """(profile, was_ambiguous) for a legacy row by user+course."""
            enrolled_profile_ids = list(
                Enrollment.objects
                .filter(user=user, course_id=course_id,
                        learner_profile__isnull=False)
                .values_list("learner_profile_id", flat=True)
                .distinct()
            )
            if len(enrolled_profile_ids) == 1:
                profile = user.learner_profiles.filter(
                    id=enrolled_profile_ids[0]
                ).first()
                if profile:
                    return profile, False
            return user.default_learner_profile(), len(enrolled_profile_ids) > 1

        def process(rows, label, course_id_of):
            nonlocal assigned
            total = rows.count()
            self.stdout.write(f"\n{label}: {total} row(s) with NULL learner_profile")
            for row in rows.iterator():
                profile, was_ambiguous = resolve(row.student, course_id_of(row))
                if profile is None:
                    orphans.append((label, str(row.id), row.student.email))
                    continue
                if was_ambiguous:
                    ambiguous.append((label, str(row.id), row.student.email,
                                      profile.display_name))
                assigned += 1
                if dry:
                    self.stdout.write(
                        f"  [dry] {label} {row.id} {row.student.email} -> "
                        f"{profile.id} ({profile.display_name})"
                    )
                else:
                    row.learner_profile = profile
                    row.save(update_fields=["learner_profile"])

        with transaction.atomic():
            process(
                QuizAttempt.objects
                .filter(learner_profile__isnull=True)
                .select_related("student", "quiz__subject"),
                "QuizAttempt",
                lambda r: r.quiz.subject.course_id,
            )
            process(
                AssignmentSubmission.objects
                .filter(learner_profile__isnull=True)
                .select_related("student", "assignment__subject", "assignment__chapter"),
                "AssignmentSubmission",
                lambda r: r.assignment.subject.course_id,
            )
            if dry:
                transaction.set_rollback(True)

        self.stdout.write("\n──────── SUMMARY ────────")
        self.stdout.write(f"Assigned: {assigned}")
        self.stdout.write(
            f"AMBIGUOUS (several profiles enrolled — assigned the default, "
            f"review manually): {len(ambiguous)}"
        )
        for lbl, rid, email, name in ambiguous:
            self.stdout.write(f"   - {lbl} {rid} {email} -> {name}")
        self.stdout.write(
            f"ORPHANS (account has NO learner profile — skipped): {len(orphans)}"
        )
        for lbl, rid, email in orphans:
            self.stdout.write(f"   - {lbl} {rid} {email}")
        if dry:
            self.stdout.write("\n(dry-run — nothing written)")
