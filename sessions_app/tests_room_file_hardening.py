"""RC-031 — room-file expiry, attachment safety and quota behaviour.

See ``docs/architecture/shiksha-remote/tasks/phase-5/RC-031.md``.

What this file is defending, and why it is endpoint-level rather than a
unit test of the policy helpers:

``SessionFile.expires_at`` / ``PrivateSessionFile.expires_at`` were
**write-only**. They were set at upload by live_files_views and read by
exactly one consumer — ``purge_expired_session_files`` — which is registered
in no celery-beat entry (``config/celery.py``), no wrapper in
``config/maintenance_tasks.py`` and no crontab anywhere in the repo. So the
product's promise that room files "self-destruct after
live_file_retention_days" was enforced by nothing at all: an expired row was
still listed by the files endpoint, still carried a live URL, and that URL
still served the bytes.

``config/tests_media_security.py``'s existing coverage could not catch this
because both of its fixtures are created deliberately unexpired
(``expires_at=now + 7 days``) and it asserts against ``is_authorized()``
directly rather than fetching anything.

So every test here drives a real request through the real URL conf and
asserts on the response, and the expiry tests deliberately leave the bytes
on disk — the point is that download fails *while the file still exists*,
because the sweeper is late (or, today, never runs). A test that deleted the
file first would pass against the unfixed code.
"""
import os
import shutil
import tempfile
from datetime import date, time, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role, User, UserRole
from global_settings.models import GlobalSettings
from sessions_app.live_files_views import _safe_original_name
from sessions_app.models import (
    GroupSession, GroupSessionParticipant, PrivateSession,
    PrivateSessionFile, SessionFile, SessionParticipant,
)

MEDIA = tempfile.mkdtemp(prefix="rc031-media-")


def _write_bytes(name, payload=b"%PDF-1.4 room file bytes"):
    """Put real bytes on disk at MEDIA_ROOT/<name>.

    secure_media_view 404s on a missing file, so without this an expiry test
    would pass for the wrong reason — it has to be the expiry that denies
    the download, not the absence of the file.
    """
    path = os.path.join(MEDIA, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


@override_settings(MEDIA_ROOT=MEDIA, MEDIA_SERVED_BY_NGINX=False)
class RoomFileTestBase(APITestCase):
    """Shared cast: a group room with a host, a member and an outsider, and
    a 1:1 private session between the host and the member."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, MEDIA, True)

    @classmethod
    def setUpTestData(cls):
        Role.objects.get_or_create(name="TEACHER")
        cls.host = User.objects.create_user(
            username="rc031host", email="rc031host@test.com", password="x")
        UserRole.objects.create(
            user=cls.host, role=Role.objects.get(name="TEACHER"),
            is_active=True, is_primary=True)
        cls.member = User.objects.create_user(
            username="rc031member", email="rc031member@test.com", password="x")
        cls.outsider = User.objects.create_user(
            username="rc031out", email="rc031out@test.com", password="x")
        cls.staff = User.objects.create_user(
            username="rc031staff", email="rc031staff@test.com", password="x",
            is_staff=True)

        now = timezone.now()
        # short_code is unique=True with default="" — two fixtures created
        # without one collide on the empty string, so set it explicitly.
        cls.group = GroupSession.objects.create(
            host=cls.host, subject_name="Physics", course_title="C10",
            topic="Topic", scheduled_date=(now + timedelta(days=1)).date(),
            scheduled_time=time(15, 0), duration_minutes=45,
            status="scheduled", short_code="RC031A")
        cls.membership = GroupSessionParticipant.objects.create(
            session=cls.group, user=cls.member)

        cls.private = PrivateSession.objects.create(
            teacher=cls.host, requested_by=cls.member, subject="Mathematics",
            scheduled_date=date.today() + timedelta(days=1),
            scheduled_time=time(14, 0), duration_minutes=60,
            session_type="one_on_one", group_strength=1, status="pending")
        SessionParticipant.objects.create(
            session=cls.private, user=cls.member, role="student")

    def _group_file(self, name, expires_in_days):
        _write_bytes(name)
        return SessionFile.objects.create(
            session=self.group, uploaded_by=self.host, file=name,
            original_name=os.path.basename(name), size_bytes=24,
            expires_at=timezone.now() + timedelta(days=expires_in_days))

    def _private_file(self, name, expires_in_days):
        _write_bytes(name)
        return PrivateSessionFile.objects.create(
            session=self.private, uploaded_by=self.host, file=name,
            original_name=os.path.basename(name), size_bytes=24,
            expires_at=timezone.now() + timedelta(days=expires_in_days))

    def _download(self, user, row):
        self.client.force_authenticate(user=user)
        return self.client.get(f"/api/media/secure/{row.file.name}")


class RoomFileExpiryDownloadTest(RoomFileTestBase):
    """The headline: an expired room file must not serve its bytes, even
    though the row and the file are both still there."""

    def setUp(self):
        super().setUp()
        self.live = self._group_file("session_files/2026/08/20/live.pdf", 7)
        self.dead = self._group_file("session_files/2026/08/20/dead.pdf", -1)
        self.private_dead = self._private_file(
            "session_files/2026/08/20/private-dead.pdf", -1)

    def test_room_member_can_download_a_live_file(self):
        """Baseline that must keep passing — the fix denies expired files,
        not room files."""
        self.assertEqual(self._download(self.member, self.live).status_code, 200)

    def test_host_can_download_a_live_file(self):
        self.assertEqual(self._download(self.host, self.live).status_code, 200)

    def test_expired_file_is_denied_although_its_bytes_are_still_on_disk(self):
        # Assert the preconditions explicitly: this test is worthless unless
        # the sweeper really has NOT run yet.
        self.assertTrue(SessionFile.objects.filter(pk=self.dead.pk).exists())
        self.assertTrue(os.path.isfile(os.path.join(MEDIA, self.dead.file.name)))

        self.assertEqual(self._download(self.member, self.dead).status_code, 404)

    def test_expired_file_is_denied_to_the_host_too(self):
        self.assertEqual(self._download(self.host, self.dead).status_code, 404)

    def test_expired_file_is_denied_to_staff_too(self):
        """Retention is a promise to the people in the room, so no role
        outlives it — staff keep review access to LIVE room files only."""
        self.assertEqual(self._download(self.staff, self.dead).status_code, 404)
        self.assertEqual(self._download(self.staff, self.live).status_code, 200)

    def test_expired_private_file_is_denied_to_its_own_recipient(self):
        self.assertEqual(
            self._download(self.member, self.private_dead).status_code, 404)

    def test_outsider_is_denied_a_live_file(self):
        self.assertEqual(self._download(self.outsider, self.live).status_code, 404)

    def test_anonymous_is_denied_a_live_file(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(
            self.client.get(f"/api/media/secure/{self.live.file.name}").status_code,
            404)

    def test_a_participant_who_left_the_room_is_denied(self):
        """Stale presence: GroupSessionParticipant.left_at is the only
        signal, and it is set when the room ends. Once it is set, the
        membership clause must stop passing."""
        self.membership.left_at = timezone.now()
        self.membership.save(update_fields=["left_at"])
        self.assertEqual(self._download(self.member, self.live).status_code, 404)


class PrivateFileRecipientOnlyTest(RoomFileTestBase):
    """A 1:1 session's files stay recipient-only — including from staff."""

    def setUp(self):
        super().setUp()
        self.row = self._private_file("session_files/2026/08/21/hw.pdf", 7)

    def test_teacher_and_requester_can_download(self):
        self.assertEqual(self._download(self.host, self.row).status_code, 200)
        self.assertEqual(self._download(self.member, self.row).status_code, 200)

    def test_outsider_is_denied(self):
        self.assertEqual(self._download(self.outsider, self.row).status_code, 404)

    def test_staff_is_denied(self):
        """🔴 Behaviour change (RC-031). _check_session_file used to return
        True for any is_staff account before it looked at the model at all,
        so staff could read private tutoring files. Group-room moderation is
        unaffected — see RoomFileExpiryDownloadTest."""
        self.assertEqual(self._download(self.staff, self.row).status_code, 404)


class RoomFileListingTest(RoomFileTestBase):
    """The JSON listing and the bytes must agree about what exists."""

    def setUp(self):
        super().setUp()
        self.live = self._group_file("session_files/2026/08/22/live.pdf", 7)
        self.dead = self._group_file("session_files/2026/08/22/dead.pdf", -1)
        self.p_live = self._private_file("session_files/2026/08/22/p-live.pdf", 7)
        self.p_dead = self._private_file("session_files/2026/08/22/p-dead.pdf", -1)

    def test_group_listing_omits_expired_rows(self):
        self.client.force_authenticate(user=self.member)
        res = self.client.get(f"/api/sessions/group-sessions/{self.group.id}/files/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual([r["id"] for r in res.json()], [self.live.id])

    def test_private_listing_omits_expired_rows(self):
        self.client.force_authenticate(user=self.member)
        res = self.client.get(f"/api/sessions/{self.private.id}/files/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual([r["id"] for r in res.json()], [self.p_live.id])

    def test_outsider_cannot_list_at_all(self):
        self.client.force_authenticate(user=self.outsider)
        res = self.client.get(f"/api/sessions/group-sessions/{self.group.id}/files/")
        self.assertEqual(res.status_code, 403)


class RoomFileQuotaTest(RoomFileTestBase):
    """live_max_files_per_session, and what counts toward it."""

    def setUp(self):
        super().setUp()
        s = GlobalSettings.load()
        s.live_max_files_per_session = 2
        s.live_max_upload_mb = 5
        s.save()
        self.client.force_authenticate(user=self.member)

    def _upload(self, filename="note.pdf", payload=b"bytes", content_type="application/pdf"):
        return self.client.post(
            f"/api/sessions/group-sessions/{self.group.id}/files/",
            {"file": SimpleUploadedFile(filename, payload, content_type=content_type)},
            format="multipart")

    def test_upload_is_refused_once_the_cap_is_reached(self):
        self.assertEqual(self._upload("a.pdf").status_code, 201)
        self.assertEqual(self._upload("b.pdf").status_code, 201)
        refused = self._upload("c.pdf")
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["code"], "too_many")

    def test_expired_rows_do_not_occupy_the_quota(self):
        """An expired file is gone from every read path, so it must not keep
        wedging uploads — otherwise any room older than the retention window
        becomes permanently unable to share a file."""
        self._group_file("session_files/2026/08/23/old1.pdf", -1)
        self._group_file("session_files/2026/08/23/old2.pdf", -1)
        self.assertEqual(SessionFile.objects.filter(session=self.group).count(), 2)
        self.assertEqual(self._upload("fresh.pdf").status_code, 201)

    def test_oversized_upload_is_refused(self):
        res = self._upload("big.pdf", payload=b"x" * (6 * 1024 * 1024))
        self.assertEqual(res.status_code, 413)
        self.assertEqual(res.json()["code"], "too_large")


class RoomFileUploadSanitizationTest(RoomFileTestBase):
    """original_name is echoed to clients and drives the download filename;
    content_type is client-supplied and must not be trusted verbatim.

    ⚠️ Scope note, established by running the parser rather than assuming:
    Django's MultiPartParser ALREADY reduces "../../etc/passwd" to "passwd",
    "C:\\Users\\me\\x.pdf" to "x.pdf" and strips NUL bytes before a view ever
    sees upload.name. An endpoint test for those cases passes with
    _safe_original_name() deleted — i.e. vacuously — so they are NOT
    asserted here as if this change earned them. They are covered by the
    direct unit test at the bottom, which is honest about being
    defence-in-depth for a non-multipart caller or a future parser change.

    What Django does NOT do, and what therefore is tested through the
    endpoint below: trimming surrounding whitespace, and refusing a name
    made only of dots.
    """

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.member)

    def _upload(self, filename, content_type="application/pdf"):
        res = self.client.post(
            f"/api/sessions/group-sessions/{self.group.id}/files/",
            {"file": SimpleUploadedFile(filename, b"bytes", content_type=content_type)},
            format="multipart")
        self.assertEqual(res.status_code, 201, res.content)
        return SessionFile.objects.get(pk=res.json()["id"])

    def test_a_dots_only_name_never_survives(self):
        """Django passes "...." through untouched; it must not become a
        download filename."""
        self.assertEqual(self._upload("....").original_name, "file")

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(self._upload("  spaced.pdf  ").original_name, "spaced.pdf")

    def test_a_bogus_client_content_type_is_discarded(self):
        """The old code stored the multipart header verbatim, truncated to
        120 chars — an attacker-chosen string in a field other code may
        later put in a header or a template."""
        self.assertEqual(self._upload("ok.pdf", content_type="not a mime type").content_type, "")

    def test_a_well_formed_client_content_type_is_kept(self):
        self.assertEqual(self._upload("ok.pdf").content_type, "application/pdf")

    def test_safe_original_name_unit_cases(self):
        """Direct unit coverage for the inputs Django's parser already
        neutralises today. Kept because _safe_original_name is the only
        thing guaranteeing it for a caller that is not DRF multipart, but
        deliberately NOT asserted through the endpoint, where it would pass
        whether or not the sanitiser existed."""
        self.assertEqual(_safe_original_name("../../etc/passwd"), "passwd")
        self.assertEqual(_safe_original_name("C:\\Users\\me\\secret.pdf"), "secret.pdf")
        self.assertEqual(_safe_original_name("a\x00b\x1fc.pdf"), "abc.pdf")
        self.assertEqual(_safe_original_name("note\r\nX-Evil: 1.pdf"), "noteX-Evil: 1.pdf")
        self.assertEqual(_safe_original_name(""), "file")
        self.assertEqual(_safe_original_name(None), "file")
        self.assertEqual(len(_safe_original_name("x" * 400)), 255)


@override_settings(MEDIA_ROOT=MEDIA, MEDIA_SERVED_BY_NGINX=False)
class SecureMediaAttachmentSafetyTest(RoomFileTestBase):
    """Private media is user-uploaded and served from the API's own origin,
    so anything scriptable must never render in place."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.member)

    def _get(self, basename):
        row = self._group_file(f"session_files/2026/08/24/{basename}", 7)
        return self.client.get(f"/api/media/secure/{row.file.name}")

    def test_pdf_is_served_inline_with_nosniff(self):
        res = self._get("notes.pdf")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertTrue(res["Content-Disposition"].startswith("inline;"))
        self.assertEqual(res["X-Content-Type-Options"], "nosniff")

    def test_html_is_forced_to_an_octet_stream_attachment(self):
        res = self._get("payload.html")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/octet-stream")
        self.assertTrue(res["Content-Disposition"].startswith("attachment;"))

    def test_svg_is_never_inline_despite_being_an_image(self):
        res = self._get("logo.svg")
        self.assertEqual(res["Content-Type"], "application/octet-stream")
        self.assertTrue(res["Content-Disposition"].startswith("attachment;"))

    def test_an_unknown_extension_is_forced_to_attachment(self):
        res = self._get("thing.weirdext")
        self.assertEqual(res["Content-Type"], "application/octet-stream")
        self.assertTrue(res["Content-Disposition"].startswith("attachment;"))

    def test_filename_is_rfc5987_encoded(self):
        res = self._get("my notes.pdf")
        self.assertIn("filename*=UTF-8''my%20notes.pdf", res["Content-Disposition"])

    @override_settings(MEDIA_SERVED_BY_NGINX=True)
    def test_the_nginx_accel_branch_carries_the_same_headers(self):
        """nginx builds the body-carrying response from this header set, so
        the safety headers must be present on the X-Accel-Redirect response
        too — not only on the local FileResponse path the tests usually
        exercise."""
        res = self._get("payload.html")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res["X-Accel-Redirect"].startswith("/internal-media/"))
        self.assertEqual(res["Content-Type"], "application/octet-stream")
        self.assertTrue(res["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(res["X-Content-Type-Options"], "nosniff")
