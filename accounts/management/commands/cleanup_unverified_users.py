from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User


class Command(BaseCommand):
    help = (
        "Delete unverified users whose signup is older than the token expiry "
        "(24h by default). Frees stuck email addresses and trims orphan rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=24,
            help="Age threshold in hours (default: 24, matching token expiry).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be deleted without deleting.",
        )

    def handle(self, *args, **opts):
        cutoff = timezone.now() - timedelta(hours=opts["hours"])
        qs = User.objects.filter(is_verified=False, date_joined__lt=cutoff)

        count = qs.count()
        if opts["dry_run"]:
            for u in qs[:50]:
                self.stdout.write(f"would delete: {u.email} (joined {u.date_joined:%Y-%m-%d %H:%M})")
            self.stdout.write(self.style.WARNING(f"DRY RUN: {count} unverified users older than {opts['hours']}h"))
            return

        deleted, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} unverified users older than {opts['hours']}h"))
