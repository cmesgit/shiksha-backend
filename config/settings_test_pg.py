"""Test settings on REAL PostgreSQL 16 — for the concurrency harness only.

🔴 **Why this file has to exist.** `config/settings_test.py` runs SQLite, and
SQLite makes `select_for_update()` a **silent no-op**. Every row-locking
guarantee in the collaboration work is therefore unproven under the default
suite: deleting the lock fails no test. Three tasks have now shipped owing a
race proof —

    RC-005  one active grant per device, across two rooms at once
    RC-031  the atomic room-file upload quota
    RC-007b two simultaneous credential refreshes

— and each evidence file says the same thing: "add it to the PG-16 harness
RC-008 has to build anyway". This is that harness.

**It is not a replacement for `settings_test`.** The ordinary suite stays on
SQLite because it is fast and because the deployed behaviour it checks does
not depend on locking. Run this one for the race tests, and in CI before a
release gate.

Usage:

    createdb shiksha_rc_pg            # once; peer auth as the local user
    DJANGO_SETTINGS_MODULE=config.settings_test_pg \\
      .venv/bin/python manage.py test sessions_app.tests_collaboration_races

The deployed target is PostgreSQL **16.15** on both dev and prod (RC-004
measured it), and this box runs 16.15 too — so the lock semantics being
exercised here are the real ones, not an approximation.
"""
import os

from .settings_test import *  # noqa: F401,F403


class _NoMigrations:
    """Build the schema straight from the models, skipping the migration chain.

    🔴 **A deliberate scope limit, stated so nobody mistakes it for a wider
    claim.** This harness exists to prove *runtime row-locking semantics* on
    real PostgreSQL. It does **not** prove the migration chain applies on
    PostgreSQL — and in fact it cannot today, because an unrelated app
    (`quizzes`) has a migration that is idempotent on SQLite and not on PG:
    applying the full chain fails with `column "explanation" of relation
    "quizzes_question" already exists`.

    That is a **real, pre-existing repo defect** worth its own task; it is
    not RC-008's to fix and faking it silently would be worse. Tables here
    come from the current model state, which is what carries the
    `CheckConstraint`s, partial unique indexes and FKs the races turn on —
    so the locking being exercised is the real thing.
    """

    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = _NoMigrations()

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PG_TEST_DB", "shiksha_rc_pg"),
        # Peer auth over the unix socket: no password in a settings file, and
        # no network listener involved.
        "USER": os.environ.get("PG_TEST_USER", ""),
        "PASSWORD": os.environ.get("PG_TEST_PASSWORD", ""),
        "HOST": os.environ.get("PG_TEST_HOST", ""),
        "PORT": os.environ.get("PG_TEST_PORT", ""),
        "TEST": {"NAME": os.environ.get("PG_TEST_NAME", "test_shiksha_rc_pg")},
        # 🔴 Must be 0 here. A persistent connection shared across the threads
        # these tests spawn would serialise them and the race would never
        # occur — the harness would pass by never running the scenario.
        "CONN_MAX_AGE": 0,
    }
}
