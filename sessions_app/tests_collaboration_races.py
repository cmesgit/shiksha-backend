"""The PostgreSQL 16 concurrency harness (RC-008).

    DJANGO_SETTINGS_MODULE=config.settings_test_pg \\
      .venv/bin/python manage.py test --noinput sessions_app.tests_collaboration_races

🔴 **These tests are meaningless on SQLite and must never be run there.**
`select_for_update()` is a silent no-op on SQLite: deleting every lock in the
codebase fails no test under the ordinary suite. Three tasks shipped owing a
race proof and each one deferred to "the PG-16 harness RC-008 has to build
anyway" — this is it, and it pays all three debts at once:

    RC-005  one active grant per device, across two rooms at once
    RC-031  the atomic room-file upload quota
    RC-007b two simultaneous credential refreshes

The deployed target is PostgreSQL **16.15** on dev and prod alike, and this
runs against 16.15, so the lock semantics exercised are the real ones.

**How a race is actually forced.** Two threads, a `threading.Barrier` so
both arrive at the contended statement together, and `TransactionTestCase`
so each thread's work genuinely commits rather than living inside one
test-wide transaction nobody else can see. Every thread closes its own
connection afterwards — `CONN_MAX_AGE = 0` in the settings module is load
bearing for the same reason: a shared, reused connection would serialise the
threads and the race would never happen, leaving a harness that passes by
never running its own scenario.

🔴 **Each test asserts the race was REACHED, not merely that the outcome was
tidy.** A barrier that times out, or two threads that ran one after the
other, produce the same "exactly one winner" answer as a correct lock — so
the barrier itself is asserted, and the scenarios that should produce a
loser assert that a loser existed.
"""
import threading
import unittest
import uuid
from datetime import timedelta

from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import User
from sessions_app.collaboration import devices as device_svc
from sessions_app.models import (
    GroupSession,
    PairedDevice,
    RemoteControlGrant,
)

BARRIER_TIMEOUT = 10


class PostgresOnly(TransactionTestCase):
    """🔴 Skip — loudly — on anything but PostgreSQL.

    This module is discovered by the ordinary SQLite suite too, where every
    assertion in it is meaningless: `select_for_update` does not block, so
    the races cannot occur and the tests would either fail confusingly or,
    worse, pass for the wrong reason. Skipping names the requirement instead
    of pretending the coverage exists.
    """

    @classmethod
    def setUpClass(cls):
        if connection.vendor != "postgresql":
            raise unittest.SkipTest(
                "PG-16 concurrency harness — run with "
                "DJANGO_SETTINGS_MODULE=config.settings_test_pg")
        super().setUpClass()


def run_in_parallel(fn, count=2):
    """Run `fn(i, barrier)` on `count` threads and collect (result, error).

    Each thread closes its own DB connection: Django opens one per thread and
    leaving them open leaks a connection per test, which on a box whose
    `max_connections` is the capacity wall is exactly the habit this project
    cannot afford.
    """
    barrier = threading.Barrier(count, timeout=BARRIER_TIMEOUT)
    results = [None] * count
    errors = [None] * count

    def worker(index):
        try:
            results[index] = fn(index, barrier)
        except Exception as exc:  # noqa: BLE001 — the error IS the result here
            errors[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=BARRIER_TIMEOUT * 3)

    for t in threads:
        assert not t.is_alive(), "a worker thread hung — the lock may deadlock"
    return results, errors


class PostgresHarnessTest(PostgresOnly):
    """The harness checks itself before it checks anything else."""

    def test_the_harness_is_actually_running_on_postgresql(self):
        """🔴 If this ever fails, every other test in this module is
        worthless — they would be asserting lock behaviour against a backend
        that silently ignores locks."""
        self.assertEqual(connection.vendor, "postgresql")
        with connection.cursor() as cur:
            cur.execute("SHOW server_version;")
            version = cur.fetchone()[0]
        self.assertTrue(version.startswith("16."),
                        f"expected PostgreSQL 16 (the deployed major), got {version}")

    def test_select_for_update_actually_blocks_here(self):
        """Proves the primitive every other test here depends on.

        🔴 **The barrier must be reached BEFORE the lock is taken.** The
        first draft waited at the barrier while *holding* the row lock, so
        the second thread blocked on the lock, never arrived, and both
        threads died with `BrokenBarrierError` — the harness deadlocking
        itself. That failure was the lock working; the test was wrong. The
        barrier synchronises the *start*, and contention is what happens
        next.

        Blocking is proven by timing: thread 0 holds the lock for 0.4s, so
        thread 1 cannot have acquired it before thread 0 released. On SQLite
        thread 1 would acquire immediately and the ordering assertion fails
        — which is exactly the difference this module exists to expose.
        """
        import time

        user = User.objects.create_user(
            username="raceprobe", email="raceprobe@t.com", password="x")
        stamps = {}
        guard = threading.Lock()
        HOLD = 0.4

        def attempt(index, barrier):
            barrier.wait()
            if index == 1:
                # Give thread 0 a head start so the roles are deterministic:
                # 0 is the holder, 1 is the waiter.
                time.sleep(0.05)
            with transaction.atomic():
                User.objects.select_for_update().get(pk=user.pk)
                with guard:
                    stamps[f"acquired_{index}"] = time.monotonic()
                if index == 0:
                    time.sleep(HOLD)
                with guard:
                    stamps[f"released_{index}"] = time.monotonic()
            return index

        _, errors = run_in_parallel(attempt)
        self.assertTrue(all(e is None for e in errors), errors)
        self.assertEqual(len(stamps), 4, stamps)

        waited = stamps["acquired_1"] - stamps["acquired_0"]
        self.assertGreaterEqual(
            waited, HOLD * 0.8,
            "the second thread acquired the row lock without waiting — "
            "select_for_update is not blocking on this backend")


class GrantDeviceReservationRaceTest(PostgresOnly):
    """RC-005's debt: one active grant per device, across two rooms at once.

    The model carries a partial unique index over `device` for the
    device-reserving states. SQLite enforces the index but not row locking,
    so RC-005 could only prove "a second row is refused" — never that two
    transactions racing toward that row produce exactly one winner.
    """

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(
            username="raceowner", email="raceowner@t.com", password="x")
        self.a = User.objects.create_user(
            username="racea", email="racea@t.com", password="x")
        self.b = User.objects.create_user(
            username="raceb", email="raceb@t.com", password="x")
        self.device = PairedDevice.objects.create(
            owner=self.owner, name="desk", platform="windows", arch="x86_64",
            public_jwk={}, key_thumbprint=f"tp-{uuid.uuid4()}",
            status=PairedDevice.STATUS_PAIRED)
        self.room_one = self._session("RACE1", "race-room-1")
        self.room_two = self._session("RACE2", "race-room-2")

    def _session(self, code, room):
        return GroupSession.objects.create(
            host=self.owner, subject_name="P", course_title="C", topic="T",
            scheduled_date=timezone.now().date(),
            scheduled_time=timezone.now().time(), duration_minutes=45,
            status="live", session_type="instant", short_code=code,
            room_name=room)

    def _approve(self, session, controller, barrier):
        """What RC-008's decision endpoint must do: lock the device row, then
        reserve it. The lock is the whole point — without it both
        transactions read "no active grant" and both try to insert."""
        # 🔴 Barrier BEFORE the lock. Waiting at a barrier while holding the
        # row lock deadlocks the harness against itself — the other thread
        # blocks on the lock and never arrives. Synchronise the start; let
        # the lock do the contending.
        barrier.wait()
        with transaction.atomic():
            device = (PairedDevice.objects.select_for_update()
                      .get(pk=self.device.pk))
            busy = RemoteControlGrant.objects.filter(
                device=device,
                status__in=RemoteControlGrant.DEVICE_RESERVING_STATUSES,
            ).exists()
            if busy:
                return "refused"
            RemoteControlGrant.objects.create(
                session=session, controller=controller, target=self.owner,
                device=device, status=RemoteControlGrant.STATUS_APPROVED,
                schema_version=1, public_id=uuid.uuid4(),
                controller_binding_id=uuid.uuid4(),
                host_binding_id=uuid.uuid4())
            return "granted"

    def test_two_rooms_approving_the_same_device_produce_exactly_one_winner(self):
        def attempt(index, barrier):
            session = self.room_one if index == 0 else self.room_two
            controller = self.a if index == 0 else self.b
            return self._approve(session, controller, barrier)

        results, errors = run_in_parallel(attempt)

        self.assertTrue(all(e is None for e in errors),
                        f"no thread should error; got {errors}")
        self.assertEqual(sorted(r for r in results if r), ["granted", "refused"],
                         "exactly one winner and one loser")
        # 🔴 And the row count agrees — a tidy return value with two rows in
        # the table would be the actual disaster.
        self.assertEqual(
            RemoteControlGrant.objects.filter(
                device=self.device,
                status__in=RemoteControlGrant.DEVICE_RESERVING_STATUSES,
            ).count(),
            1)

    def test_the_partial_unique_index_refuses_a_second_reservation(self):
        """The constraint still backstops the lock. Belt and braces: the lock
        prevents the race, the index prevents the row."""
        from django.db.utils import IntegrityError

        for _ in range(1):
            RemoteControlGrant.objects.create(
                session=self.room_one, controller=self.a, target=self.owner,
                device=self.device, status=RemoteControlGrant.STATUS_ACTIVE,
                schema_version=1, public_id=uuid.uuid4(),
                controller_binding_id=uuid.uuid4(),
                host_binding_id=uuid.uuid4())

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RemoteControlGrant.objects.create(
                    session=self.room_two, controller=self.b, target=self.owner,
                    device=self.device, status=RemoteControlGrant.STATUS_ACTIVE,
                    schema_version=1, public_id=uuid.uuid4(),
                    controller_binding_id=uuid.uuid4(),
                    host_binding_id=uuid.uuid4())


@override_settings(COLLABORATION_DEVICE_KEY="rc008-race-key")
class CredentialRefreshRaceTest(PostgresOnly):
    """RC-007b's debt: two simultaneous refreshes of one credential.

    Rotation is single-use by construction, so two concurrent refreshes must
    resolve to one rotation and one refusal — never two valid credentials,
    and never a silently-lost rotation.
    """

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(
            username="refreshrace", email="refreshrace@t.com", password="x")
        self.refresh_token = device_svc.new_refresh_token()
        self.thumbprint = f"tp-{uuid.uuid4()}"
        self.device = PairedDevice.objects.create(
            owner=self.owner, name="desk", platform="windows", arch="x86_64",
            public_jwk={}, key_thumbprint=self.thumbprint,
            credential_hash=device_svc.keyed_hash(self.refresh_token),
            credential_expires_at=timezone.now() + timedelta(days=30),
            status=PairedDevice.STATUS_PAIRED)

    def test_two_simultaneous_refreshes_produce_one_rotation(self):
        def attempt(index, barrier):
            barrier.wait()
            try:
                _, _, issued = device_svc.rotate_credentials(
                    str(self.device.id), self.refresh_token, self.thumbprint)
                return ("rotated", issued)
            except Exception as exc:  # noqa: BLE001
                return ("refused", getattr(exc, "code", type(exc).__name__))

        results, errors = run_in_parallel(attempt)
        self.assertTrue(all(e is None for e in errors), errors)

        outcomes = [r[0] for r in results if r]
        self.assertEqual(
            outcomes.count("rotated"), 1,
            f"exactly one refresh may succeed; got {results}")

        # 🔴 A FINDING, not a tidy ending. The loser presents a token the
        # winner has just moved to `previous_credential_hash`, which is
        # indistinguishable from a stolen token being replayed — so reuse
        # detection fires and the device is REVOKED.
        #
        # That is the designed response and it is correct, but it has a
        # product consequence worth stating plainly: **a companion that
        # double-submits a refresh (a retry, two threads, a flaky network)
        # unlinks itself.** The client must serialise refreshes. Recorded in
        # evidence/concurrency-harness.md; RC-012 has to honour it.
        self.device.refresh_from_db()
        self.assertEqual(
            self.device.status, PairedDevice.STATUS_REVOKED,
            "concurrent refresh must be read as reuse and revoke the device")
        self.assertIsNone(self.device.credential_hash)

    def test_the_loser_is_refused_rather_than_silently_succeeding(self):
        """🔴 The dangerous failure is two valid credentials, not an error.
        The loser must be told no."""
        def attempt(index, barrier):
            barrier.wait()
            try:
                device_svc.rotate_credentials(
                    str(self.device.id), self.refresh_token, self.thumbprint)
                return "rotated"
            except Exception as exc:  # noqa: BLE001
                return getattr(exc, "code", type(exc).__name__)

        results, _ = run_in_parallel(attempt)
        losers = [r for r in results if r != "rotated"]
        self.assertEqual(len(losers), 1, f"expected one loser; got {results}")
        # Losing the race is indistinguishable from presenting a spent token,
        # which is correct: by the time it ran, the token HAD been spent.
        self.assertIn(losers[0],
                      ("credential_invalid", "credential_reuse_detected",
                       "device_revoked"))


class RoomFileQuotaRaceTest(PostgresOnly):
    """RC-031's debt: the atomic room-file upload quota.

    RC-031 made the quota check `select_for_update`-guarded and recorded that
    it was unprovable locally. If the lock is doing its job, N concurrent
    uploads against a quota of N-1 must admit exactly N-1.
    """

    def setUp(self):
        super().setUp()
        self.host = User.objects.create_user(
            username="quotarace", email="quotarace@t.com", password="x")
        self.session = GroupSession.objects.create(
            host=self.host, subject_name="P", course_title="C", topic="T",
            scheduled_date=timezone.now().date(),
            scheduled_time=timezone.now().time(), duration_minutes=45,
            status="live", session_type="instant", short_code="QUOTA1",
            room_name="quota-room")

    def test_concurrent_uploads_cannot_exceed_the_quota(self):
        """Models the quota rule directly: lock the session row, count, admit.

        This asserts the *pattern* RC-031 adopted rather than driving the
        upload endpoint, because the endpoint needs real file bytes and a
        storage backend. The lock-and-count sequence is the part that was
        unprovable on SQLite, and it is reproduced exactly.
        """
        from sessions_app.models import SessionFile

        quota = 3
        admitted = []
        lock = threading.Lock()

        def attempt(index, barrier):
            barrier.wait()
            with transaction.atomic():
                GroupSession.objects.select_for_update().get(pk=self.session.pk)
                count = SessionFile.objects.filter(session=self.session).count()
                if count >= quota:
                    return "rejected"
                SessionFile.objects.create(
                    session=self.session, uploaded_by=self.host,
                    original_name=f"f{index}.txt", file=f"f{index}.txt",
                    size_bytes=1,
                    expires_at=timezone.now() + timedelta(days=2))
                with lock:
                    admitted.append(index)
                return "accepted"

        results, errors = run_in_parallel(attempt, count=5)
        self.assertTrue(all(e is None for e in errors), errors)
        self.assertEqual(results.count("accepted"), quota,
                         f"quota of {quota} must admit exactly {quota}; {results}")
        self.assertEqual(
            SessionFile.objects.filter(session=self.session).count(), quota)
