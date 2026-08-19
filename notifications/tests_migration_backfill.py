# Proves migration 0008's backfill actually re-tracks PRE-EXISTING rows.
#
# The ordinary test suite can never catch a broken backfill: Django builds
# the test database by running every migration against an EMPTY schema, so
# the RunPython step always sees zero rows and always "passes". This test
# rewinds to 0007, inserts rows the way production has them, then rolls
# forward and asserts the data actually moved.
#
# It matters because the dev database alone has ~52 rows (10 of them
# quiz.reminder, which must become academy) and prod has more. If the
# backfill silently no-ops, every historical notification stays NEUTRAL and
# therefore shows in BOTH bells — the exact leak this feature removes,
# invisible in tests and visible only to users.

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

APP = "notifications"
BEFORE = "0007_notificationpreference_language"
AFTER = "0008_notification_track_and_more"


class TrackBackfillMigrationTest(TransactionTestCase):
    # The migration machinery needs to rewrite the real schema, so this
    # cannot be a plain TestCase (which wraps everything in one atomic
    # block and forbids the DDL).
    available_apps = None

    def _migrate_to(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([(APP, target)])
        executor.loader.build_graph()
        return executor.loader.project_state([(APP, target)]).apps

    def tearDown(self):
        # Leave the schema at HEAD so later tests in the same process
        # aren't handed a rewound database.
        self._migrate_to(AFTER)

    def test_backfill_retracks_existing_rows(self):
        old_apps = self._migrate_to(BEFORE)
        Notification = old_apps.get_model(APP, "Notification")

        # Only `notifications` is rewound, so the accounts TABLE is still at
        # HEAD and the real User model matches it. Using the historical
        # accounts model here would not: its field list is resolved from a
        # different point in the graph than the live schema, which is what
        # made the first two attempts at this test blow up on
        # accepted_terms_version. Only the recipient FK matters anyway.
        from accounts.models import User
        user = User.objects.create(username="backfill", email="b@example.com")

        # The exact verb mix present on the dev box today, plus one of each
        # interesting edge: the payments.receipt exception, a skill row, and
        # an unmapped verb that must stay neutral.
        for verb in ("forum.reply", "chat.message", "quiz.reminder",
                     "forum.thread", "forum.accepted", "skill.confirmed",
                     "payments.receipt", "payments.failed", "brandnew.event"):
            # recipient_id, not recipient: the historical Notification class
            # will not accept a real User instance (different model class
            # for the same table).
            Notification.objects.create(
                recipient_id=user.pk, verb=verb, title=verb, body="",
                link_url="", payload={}, audience_role="",
                audience_identity="",
            )

        new_apps = self._migrate_to(AFTER)
        Notification = new_apps.get_model(APP, "Notification")
        tracked = dict(Notification.objects.values_list("verb", "track"))

        self.assertEqual(tracked["quiz.reminder"], "academy")
        self.assertEqual(tracked["skill.confirmed"], "skill")
        self.assertEqual(tracked["payments.receipt"], "academy")

        # Everything cross-track must stay blank — a backfill that
        # over-reaches would start hiding forum replies and DMs from one
        # of the two bells.
        for verb in ("forum.reply", "chat.message", "forum.thread",
                     "forum.accepted", "payments.failed", "brandnew.event"):
            self.assertEqual(tracked[verb], "", f"{verb} should stay neutral")
