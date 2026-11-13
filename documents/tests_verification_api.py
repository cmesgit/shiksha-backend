"""Phase 4 — the verification API.

The rules themselves are covered in `tests_verification.py`. What this file
covers is everything that only exists at the HTTP boundary:

  * the feature gate, which must hide the endpoints entirely while off;
  * the admin bar, which is superuser-or-ADMIN and deliberately NOT is_staff;
  * the shapes the screens actually read, since a field that reaches the model
    but not the serializer is the trap this codebase has hit six times.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from global_settings.models import GlobalSettings

from . import verification as v
from .models import (
    Document,
    DocumentTopic,
    InviteState,
    SpecializationSource,
    SpecializationStatus,
    SubjectSpecialization,
    VerificationInvite,
    VerificationReview,
    VerificationSettings,
    VerificationStatus,
)

User = get_user_model()
BASE = "/api/explore/verification"


def _user(name, **kw):
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", password="pw12345!", **kw)


def _client(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user=user)
    return c


class VerificationApiTestCase(TestCase):
    def setUp(self):
        gs = GlobalSettings.load()
        gs.explore_verification_enabled = True
        gs.save()
        self.vsettings = VerificationSettings.load()
        self.owner = _user("api_owner")

    def _set(self, *subjects, owner=None, title="A set", **kw):
        doc = Document.objects.create(
            owner=owner or self.owner, title=title, **kw)
        doc.topics.all().delete()
        for i, subject in enumerate(subjects):
            DocumentTopic.objects.create(
                document=doc, subject=subject,
                field=v.field_for_subject(subject), order=i)
        return doc

    def _specialist(self, name, *subjects):
        user = _user(name)
        for subject in subjects:
            SubjectSpecialization.objects.create(
                user=user, subject=subject,
                status=SpecializationStatus.CONFIRMED,
                source=SpecializationSource.APPROVED)
        return user


class FeatureGateTest(VerificationApiTestCase):
    """While the flag is off there must be nothing here at all.

    ⚠ 404, not 403. A 403 confirms the endpoint exists to anyone who probes
    for it, which is information about an unreleased feature. The flag is a
    real gate, not a UI preference.
    """

    def setUp(self):
        super().setUp()
        gs = GlobalSettings.load()
        gs.explore_verification_enabled = False
        gs.save()

    def test_the_member_endpoints_are_absent(self):
        client = _client(self._specialist("gate1", "Physics"))
        for path in (f"{BASE}/", f"{BASE}/queue/", f"{BASE}/notifications/"):
            self.assertEqual(client.get(path).status_code, 404, path)

    def test_the_admin_endpoints_are_absent(self):
        admin = _user("gate_admin", is_superuser=True)
        self.assertEqual(
            _client(admin).get(f"{BASE}/admin/settings/").status_code, 404)

    def test_reviews_cannot_be_submitted(self):
        doc = self._set("Physics")
        reviewer = self._specialist("gate2", "Physics")
        res = _client(reviewer).post(
            f"{BASE}/sets/{doc.id}/review/",
            {"matches_tags": True, "content_accurate": True,
             "files_complete": True}, format="json")
        self.assertEqual(res.status_code, 404)

    def test_turning_it_on_needs_no_deploy(self):
        """The whole point of the flag living in the database."""
        gs = GlobalSettings.load()
        gs.explore_verification_enabled = True
        gs.save()
        client = _client(self._specialist("gate3", "Physics"))
        self.assertEqual(client.get(f"{BASE}/").status_code, 200)


class AuthTest(VerificationApiTestCase):
    def test_an_anonymous_visitor_is_refused(self):
        self.assertEqual(_client().get(f"{BASE}/").status_code, 401)

    def test_a_signed_in_member_with_no_subjects_gets_an_empty_queue(self):
        """Not an error — this is most members. The page explains how to
        become a specialist rather than showing them a failure."""
        res = _client(_user("auth1")).get(f"{BASE}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["queue"], [])
        self.assertEqual(res.json()["my_subjects"], [])


class QueueApiTest(VerificationApiTestCase):
    def test_the_queue_carries_what_the_row_renders(self):
        """The design's row is "<uploader> · N files · waiting 6 days" plus a
        status pill and tag chips. Every one of those is a field here, and a
        missing one renders as a blank in the middle of a sentence."""
        doc = self._set("Physics", title="Lab sheets")
        reviewer = self._specialist("queue1", "Physics")
        row = _client(reviewer).get(f"{BASE}/queue/").json()["results"][0]
        for key in ("title", "uploader", "file_count", "waiting", "tags",
                    "progress"):
            self.assertIn(key, row)
        self.assertEqual(row["title"], "Lab sheets")
        self.assertEqual(row["progress"]["label"], "0 of 3 confirmations")

    def test_an_invite_shows_its_deadline(self):
        doc = self._set("Physics")
        reviewer = self._specialist("queue2", "Physics")
        VerificationInvite.objects.create(
            document=doc, version=1, reviewer=reviewer, subject="Physics",
            state=InviteState.OPEN,
            expires_at=timezone.now() + timezone.timedelta(days=5))
        row = _client(reviewer).get(f"{BASE}/queue/").json()["results"][0]
        self.assertEqual(row["invite"]["days_left"], 4)

    def test_no_invite_is_null_rather_than_zero(self):
        """Absent and "0 days left" are different things — one means the set
        is in the open queue, the other that the invite is about to lapse."""
        self._set("Physics")
        reviewer = self._specialist("queue3", "Physics")
        row = _client(reviewer).get(f"{BASE}/queue/").json()["results"][0]
        self.assertIsNone(row["invite"])


class ReviewApiTest(VerificationApiTestCase):
    def test_three_yes_answers_confirm(self):
        doc = self._set("Physics")
        reviewer = self._specialist("rev1", "Physics")
        res = _client(reviewer).post(
            f"{BASE}/sets/{doc.id}/review/",
            {"matches_tags": True, "content_accurate": True,
             "files_complete": True}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["outcome"], "confirmed")
        self.assertEqual(res.json()["progress"]["confirmations"], 1)

    def test_a_no_without_a_note_is_refused_with_the_designs_wording(self):
        doc = self._set("Physics")
        reviewer = self._specialist("rev2", "Physics")
        res = _client(reviewer).post(
            f"{BASE}/sets/{doc.id}/review/",
            {"matches_tags": True, "content_accurate": False,
             "files_complete": True}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("note", res.json())

    def test_reviewing_your_own_upload_is_refused_readably(self):
        """The member is told what is true of them, not "Forbidden"."""
        doc = self._set("Physics")
        SubjectSpecialization.objects.create(
            user=self.owner, subject="Physics",
            status=SpecializationStatus.CONFIRMED)
        res = _client(self.owner).post(
            f"{BASE}/sets/{doc.id}/review/",
            {"matches_tags": True, "content_accurate": True,
             "files_complete": True}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertIn("your own", res.json()["detail"])

    def test_a_second_review_of_the_same_version_is_refused(self):
        doc = self._set("Physics")
        reviewer = self._specialist("rev3", "Physics")
        body = {"matches_tags": True, "content_accurate": True,
                "files_complete": True}
        _client(reviewer).post(f"{BASE}/sets/{doc.id}/review/", body, format="json")
        res = _client(reviewer).post(
            f"{BASE}/sets/{doc.id}/review/", body, format="json")
        self.assertEqual(res.status_code, 403)

    def test_the_review_form_reports_whether_this_viewer_may_act(self):
        """The screen must not render a live form for someone the server will
        refuse — the design has no state for "you submitted and it bounced"."""
        doc = self._set("Physics")
        outsider = _user("rev4")
        body = _client(outsider).get(f"{BASE}/sets/{doc.id}/").json()
        self.assertFalse(body["can_review"])
        self.assertIn("specialist", body["reason"])

    def test_the_form_names_the_subject_being_reviewed_in(self):
        """"Reviewing as a Civil Engineering specialist" — and for a set with
        two tags it has to be the one THIS reviewer holds."""
        doc = self._set("Civil Engineering", "Sociology")
        reviewer = self._specialist("rev5", "Sociology")
        body = _client(reviewer).get(f"{BASE}/sets/{doc.id}/").json()
        self.assertEqual(body["reviewing_as"], "Sociology")


class SpecializationApiTest(VerificationApiTestCase):
    def test_requesting_a_subject_creates_a_pending_row_not_a_grant(self):
        """⚠ THE POINT. If this confirmed on creation, anyone could make
        themselves a specialist in anything by typing it into a box."""
        res = _client(_user("spec1")).post(
            f"{BASE}/specializations/",
            {"subject": "Physics", "evidence": "BSc Physics"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["status"], SpecializationStatus.PENDING)

    def test_a_pending_request_does_not_open_the_queue(self):
        requester = _user("spec2")
        _client(requester).post(
            f"{BASE}/specializations/", {"subject": "Physics"}, format="json")
        self._set("Physics")
        self.assertEqual(_client(requester).get(f"{BASE}/queue/").json()["count"], 0)

    def test_asking_twice_is_refused(self):
        user = _user("spec3")
        _client(user).post(f"{BASE}/specializations/",
                           {"subject": "Physics"}, format="json")
        res = _client(user).post(f"{BASE}/specializations/",
                                 {"subject": "physics"}, format="json")
        self.assertEqual(res.status_code, 409)

    def test_requests_can_be_closed_by_the_admin(self):
        self.vsettings.members_can_request_it = False
        self.vsettings.save()
        res = _client(_user("spec4")).post(
            f"{BASE}/specializations/", {"subject": "Physics"}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_the_reason_line_explains_why_someone_is_confirmed(self):
        """The grey line under each subject. Without it the list is just a
        set of words with a green pill beside them."""
        user = self._specialist("spec5", "Physics")
        rows = _client(user).get(f"{BASE}/").json()["specializations"]
        self.assertEqual(rows[0]["reason"], "Approved by an admin")

    def test_already_held_subjects_are_not_offered_again(self):
        user = self._specialist("spec6", "Physics")
        body = _client(user).get(f"{BASE}/").json()
        self.assertNotIn("Physics", body["available_subjects"])


class MyUploadsApiTest(VerificationApiTestCase):
    def test_the_uploader_sees_the_real_state_and_the_note(self):
        """⚠ The PUBLIC sees "Unverified" for a set with changes requested —
        that is the design's rule. The UPLOADER has to see the truth, or the
        My uploads tab cannot tell them what to fix."""
        doc = self._set("Physics")
        v.record_review(
            self._specialist("mu1", "Physics"), doc, matches_tags=True,
            content_accurate=False, files_complete=True,
            note="Sheet 2 is headed Fe 500 but the lap lengths are for Fe 415.")
        row = _client(self.owner).get(f"{BASE}/").json()["my_uploads"][0]
        self.assertIn("paused", row["progress"]["label"])
        self.assertIn("Fe 500", row["reviewer_note"])

    def test_the_public_badge_never_says_changes_requested(self):
        doc = self._set("Physics")
        v.record_review(
            self._specialist("mu2", "Physics"), doc, matches_tags=False,
            content_accurate=True, files_complete=True, note="Wrong tags.")
        doc.refresh_from_db()
        badge = v.progress(doc, self.vsettings)
        from .verification_serializers import VerificationProgressSerializer
        self.assertEqual(
            VerificationProgressSerializer(badge).data["badge"], "unverified")


class AdminApiTest(VerificationApiTestCase):
    def setUp(self):
        super().setUp()
        self.admin = _user("vadmin", is_superuser=True)

    def test_a_plain_member_cannot_reach_the_admin_block(self):
        self.assertEqual(
            _client(_user("notadmin")).get(
                f"{BASE}/admin/settings/").status_code, 404)

    def test_is_staff_alone_is_not_enough(self):
        """⚠ THE POINT. Content editors carry is_staff so they can use the
        Django admin for CMS work. Letting that double as the verification
        admin bar would hand every editor the switch that decides what the
        whole library counts as checked."""
        editor = _user("editor", is_staff=True)
        self.assertEqual(
            _client(editor).get(f"{BASE}/admin/settings/").status_code, 404)

    def test_an_admin_can_read_and_change_the_settings(self):
        res = _client(self.admin).patch(
            f"{BASE}/admin/settings/",
            {"confirmations_needed": 5}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(VerificationSettings.load().confirmations_needed, 5)

    def test_the_summary_is_derived_from_the_settings(self):
        """⚠ THE POINT of computing it server-side. A hardcoded copy keeps
        saying "7 days" after someone sets it to 3, and an admin reading the
        box they were given to understand their own configuration would be
        misled by it."""
        _client(self.admin).patch(
            f"{BASE}/admin/settings/",
            {"days_to_respond": 3, "confirmations_needed": 4}, format="json")
        summary = " ".join(
            _client(self.admin).get(f"{BASE}/admin/settings/").json()["summary"])
        self.assertIn("3 days", summary)
        self.assertIn("After 4 confirmations", summary)
        self.assertNotIn("7 days", summary)

    def test_the_summary_follows_the_selection_policy(self):
        _client(self.admin).patch(
            f"{BASE}/admin/settings/",
            {"reviewer_selection": "open_queue"}, format="json")
        summary = " ".join(
            _client(self.admin).get(f"{BASE}/admin/settings/").json()["summary"])
        self.assertIn("open queue", summary)
        self.assertNotIn("invited", summary.split("open queue")[0])

    def test_approving_a_request_confirms_it(self):
        requester = _user("admreq")
        spec = SubjectSpecialization.objects.create(
            user=requester, subject="Physics",
            status=SpecializationStatus.PENDING)
        res = _client(self.admin).post(
            f"{BASE}/admin/reviewers/{spec.id}/approve/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        spec.refresh_from_db()
        self.assertEqual(spec.status, SpecializationStatus.CONFIRMED)
        self.assertEqual(spec.decided_by_id, self.admin.id)

    def test_declining_keeps_them_out_of_the_queue(self):
        requester = _user("admdec")
        spec = SubjectSpecialization.objects.create(
            user=requester, subject="Physics",
            status=SpecializationStatus.PENDING)
        _client(self.admin).post(
            f"{BASE}/admin/reviewers/{spec.id}/decline/", {}, format="json")
        self._set("Physics")
        self.assertEqual(
            _client(requester).get(f"{BASE}/queue/").json()["count"], 0)

    def test_resuming_forgives_the_missed_invites(self):
        """⚠ Without this the reviewer is paused again by the very next
        expiry and the admin's Resume button looks broken."""
        spec = SubjectSpecialization.objects.create(
            user=_user("admres"), subject="Physics",
            status=SpecializationStatus.CONFIRMED,
            is_paused=True, missed_invites=3)
        _client(self.admin).post(
            f"{BASE}/admin/reviewers/{spec.id}/resume/", {}, format="json")
        spec.refresh_from_db()
        self.assertFalse(spec.is_paused)
        self.assertEqual(spec.missed_invites, 0)

    def test_at_limit_is_derived_not_stored(self):
        self.vsettings.open_reviews_per_reviewer = 1
        self.vsettings.save()
        reviewer = self._specialist("admlim", "Physics")
        other = self._set("Physics", owner=_user("admlim_owner"))
        VerificationInvite.objects.create(
            document=other, version=1, reviewer=reviewer,
            state=InviteState.OPEN)
        rows = _client(self.admin).get(
            f"{BASE}/admin/reviewers/").json()["reviewers"]
        self.assertEqual(rows[0]["state"], "at_limit")

    def test_needs_attention_reports_what_is_flowing_normally(self):
        """"Most sets never appear here." The reassuring count is part of the
        screen, not decoration — a bare empty list reads as a broken query."""
        self._set("Physics")
        _specialist = self._specialist("admflow", "Physics")
        body = _client(self.admin).get(f"{BASE}/admin/attention/").json()
        self.assertIn("handled_automatically", body)

    def test_a_hidden_set_appears_with_every_change_request(self):
        """An admin deciding whether to restore needs both objections, not
        the most recent one."""
        doc = self._set("Physics", title="Cheat sheet")
        for i in range(2):
            v.record_review(
                self._specialist(f"admhid{i}", "Physics"), doc,
                matches_tags=False, content_accurate=True, files_complete=True,
                note=f"Objection {i}")
            doc.refresh_from_db()
        body = _client(self.admin).get(f"{BASE}/admin/attention/").json()
        row = next(r for r in body["results"] if r["title"] == "Cheat sheet")
        self.assertEqual(row["reason"], "hidden")
        self.assertEqual(len(row["change_requests"]), 2)

    def test_restoring_a_hidden_set_actually_unhides_it(self):
        """⚠ A restore that left the change requests counting would be undone
        by the next recount — the set would go straight back to hidden and the
        button would look like it did nothing."""
        doc = self._set("Physics")
        for i in range(2):
            v.record_review(
                self._specialist(f"admrest{i}", "Physics"), doc,
                matches_tags=False, content_accurate=True,
                files_complete=True, note="No.")
            doc.refresh_from_db()
        self.assertEqual(doc.verification_status, VerificationStatus.HIDDEN)
        _client(self.admin).post(
            f"{BASE}/admin/attention/{doc.id}/restore/", {}, format="json")
        doc.refresh_from_db()
        self.assertEqual(doc.verification_status, VerificationStatus.UNVERIFIED)
        self.assertEqual(v.change_request_count(doc), 0)

    def test_assigning_a_reviewer_puts_the_set_in_their_queue(self):
        """The gap the design itself flagged as not built: "assigning a
        reviewer there doesn't add a set to anyone's queue"."""
        self.vsettings.reviewer_selection = "admin_assigns"
        self.vsettings.save()
        doc = self._set("Physics")
        reviewer = self._specialist("admassign", "Physics")
        spec = SubjectSpecialization.objects.get(user=reviewer)
        res = _client(self.admin).post(
            f"{BASE}/admin/attention/{doc.id}/assign/",
            {"reviewer_id": spec.id}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            _client(reviewer).get(f"{BASE}/queue/").json()["count"], 1)

    def test_an_admin_cannot_assign_a_set_to_its_own_uploader(self):
        doc = self._set("Physics")
        spec = SubjectSpecialization.objects.create(
            user=self.owner, subject="Physics",
            status=SpecializationStatus.CONFIRMED)
        res = _client(self.admin).post(
            f"{BASE}/admin/attention/{doc.id}/assign/",
            {"reviewer_id": spec.id}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_the_reviewer_picker_offers_only_people_the_server_would_accept(self):
        doc = self._set("Physics")
        self._specialist("admpick", "Physics")
        SubjectSpecialization.objects.create(
            user=self.owner, subject="Physics",
            status=SpecializationStatus.CONFIRMED)
        rows = _client(self.admin).get(
            f"{BASE}/admin/sets/{doc.id}/reviewers/").json()
        self.assertNotIn(
            self.owner.username, [r["name"] for r in rows])


class LibraryVisibilityTest(VerificationApiTestCase):
    """What the badge and the gate do to the public library.

    Three things that are easy to get subtly wrong and hard to notice:
    the badge must be ABSENT (not "unverified") while the flag is off; a
    hidden set must leave Browse but stay visible to its owner; and turning
    off "Show unverified uploads in Browse" must not take the owner's own
    upload away from them.
    """

    LIST = "/api/explore/documents/"

    def _titles(self, user=None, **params):
        res = _client(user).get(self.LIST, params)
        self.assertEqual(res.status_code, 200, res.content)
        return [d["title"] for d in res.json()["results"]]

    def test_the_badge_reaches_the_card(self):
        """⚠ The hand-rolled-dict trap. The model has carried
        verification_status since Phase 2; until it was named in
        DocumentCardSerializer.Meta.fields no screen could see it."""
        self._set("Physics", title="Badged")
        card = _client().get(self.LIST).json()["results"][0]
        self.assertIsNotNone(card["verification"])
        self.assertEqual(card["verification"]["badge"], "unverified")
        self.assertEqual(card["verification"]["label"], "0 of 3 confirmations")

    def test_the_badge_is_absent_while_the_flag_is_off(self):
        """⚠ None, NOT "unverified". Those are different claims: one says
        nothing has checked this, the other says checking does not exist here
        yet. Shipping an amber pill onto every document in the library the day
        before launch would be the second dressed as the first."""
        gs = GlobalSettings.load()
        gs.explore_verification_enabled = False
        gs.save()
        self._set("Physics", title="Unbadged")
        card = _client().get(self.LIST).json()["results"][0]
        self.assertIsNone(card["verification"])

    def test_a_verified_set_says_so(self):
        doc = self._set("Physics", title="Good")
        for i in range(3):
            v.record_review(
                self._specialist(f"vis{i}", "Physics"), doc,
                matches_tags=True, content_accurate=True, files_complete=True)
            doc.refresh_from_db()
        card = _client().get(self.LIST).json()["results"][0]
        self.assertEqual(card["verification"]["badge"], "verified")
        self.assertEqual(card["verification"]["label"],
                         "Confirmed by 3 specialists")

    def test_changes_requested_still_reads_unverified_to_the_public(self):
        """The design: a set with changes requested "stays in Browse, marked
        Unverified". A red pill in the library would let one reviewer
        publicly brand anyone's upload."""
        doc = self._set("Physics", title="Paused")
        v.record_review(
            self._specialist("vis_cr", "Physics"), doc, matches_tags=False,
            content_accurate=True, files_complete=True, note="Wrong tags.")
        card = _client().get(self.LIST).json()["results"][0]
        self.assertEqual(card["verification"]["badge"], "unverified")
        self.assertIn("Paused", self._titles())

    def test_a_hidden_set_leaves_browse(self):
        doc = self._set("Physics", title="Hidden one")
        for i in range(2):
            v.record_review(
                self._specialist(f"vish{i}", "Physics"), doc,
                matches_tags=False, content_accurate=True,
                files_complete=True, note="No.")
            doc.refresh_from_db()
        self.assertEqual(doc.verification_status, VerificationStatus.HIDDEN)
        self.assertNotIn("Hidden one", self._titles())

    def test_but_its_owner_can_still_see_it(self):
        """⚠ THE POINT. An upload that silently vanishes gives its owner no
        route to the reviewer's note explaining how to fix it."""
        doc = self._set("Physics", title="Hidden mine")
        for i in range(2):
            v.record_review(
                self._specialist(f"viso{i}", "Physics"), doc,
                matches_tags=False, content_accurate=True,
                files_complete=True, note="No.")
            doc.refresh_from_db()
        self.assertIn("Hidden mine", self._titles(self.owner))

    def test_turning_off_show_unverified_gates_the_library(self):
        self.vsettings.show_unverified_in_browse = False
        self.vsettings.save()
        self._set("Physics", title="Not yet checked")
        self.assertNotIn("Not yet checked", self._titles())

    def test_and_the_owner_keeps_seeing_their_own(self):
        self.vsettings.show_unverified_in_browse = False
        self.vsettings.save()
        self._set("Physics", title="Mine pending")
        self.assertIn("Mine pending", self._titles(self.owner))

    def test_the_gate_does_nothing_while_the_feature_is_off(self):
        """The settings row exists before the flag is flipped. If the gate
        read it regardless, turning the feature ON later would be the first
        time anyone noticed the library had been filtered all along."""
        self.vsettings.show_unverified_in_browse = False
        self.vsettings.save()
        gs = GlobalSettings.load()
        gs.explore_verification_enabled = False
        gs.save()
        self._set("Physics", title="Still visible")
        self.assertIn("Still visible", self._titles())

    def test_the_status_filter(self):
        good = self._set("Physics", title="Verified one")
        self._set("Physics", title="Unverified one")
        for i in range(3):
            v.record_review(
                self._specialist(f"visf{i}", "Physics"), good,
                matches_tags=True, content_accurate=True, files_complete=True)
            good.refresh_from_db()
        self.assertEqual(self._titles(status="verified"), ["Verified one"])
        self.assertEqual(self._titles(status="unverified"), ["Unverified one"])
        self.assertEqual(len(self._titles(status="any")), 2)

    def test_needs_my_review_is_the_reviewers_own_queue(self):
        reviewer = self._specialist("visq", "Physics")
        self._set("Physics", title="For them")
        self._set("History", title="Not for them")
        self.assertEqual(self._titles(reviewer, status="needs_my_review"),
                         ["For them"])

    def test_needs_my_review_is_empty_for_a_guest(self):
        self._set("Physics", title="Anything")
        self.assertEqual(self._titles(status="needs_my_review"), [])

    def test_an_unknown_status_does_not_400(self):
        """Old bookmarks and hand-edited URLs reach this."""
        self._set("Physics", title="Anything")
        self.assertEqual(len(self._titles(status="banana")), 1)

    def test_the_card_tells_a_specialist_they_can_review_it(self):
        """What drives the "You specialise in X" prompt on the card. Without
        `my_subject` the prompt cannot name the subject, and a prompt that
        says "you specialise in this" explains nothing."""
        self._set("Civil Engineering", "Sociology", title="Dams")
        reviewer = self._specialist("viscard", "Sociology")
        card = _client(reviewer).get(self.LIST).json()["results"][0]
        self.assertTrue(card["verification"]["can_review"])
        self.assertEqual(card["verification"]["my_subject"], "Sociology")

    def test_and_does_not_offer_it_on_their_own_upload(self):
        SubjectSpecialization.objects.create(
            user=self.owner, subject="Physics",
            status=SpecializationStatus.CONFIRMED)
        self._set("Physics", title="Mine")
        card = _client(self.owner).get(self.LIST).json()["results"][0]
        self.assertFalse(card["verification"]["can_review"])


class SetUploadTest(VerificationApiTestCase):
    """Phase 8 — uploading several files under one title with several tags."""

    URL = "/api/explore/documents/"

    def _pdf(self, name, body=b"%PDF-1.4 x"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(name, body, content_type="application/pdf")

    def test_several_files_become_one_set(self):
        res = _client(self.owner).post(self.URL, {
            "title": "Dam resettlement",
            "file": self._pdf("a.pdf", b"%PDF-1.4 one"),
            "files": [self._pdf("b.pdf", b"%PDF-1.4 two"),
                      self._pdf("c.pdf", b"%PDF-1.4 three")],
        })
        self.assertEqual(res.status_code, 201, res.content)
        doc = Document.objects.get(pk=res.json()["id"])
        self.assertEqual(doc.files.count(), 3)
        self.assertEqual(
            [f.order for f in doc.files.all()], [0, 1, 2])

    def test_several_tag_paths_become_several_topics(self):
        """The design's own sample set is tagged under both Civil Engineering
        and Sociology, and EITHER qualifies a reviewer."""
        res = _client(self.owner).post(self.URL, {
            "title": "Dam resettlement",
            "subject": "Civil Engineering", "topic": "Water resources",
            "file": self._pdf("a.pdf"),
            "topics": ["Social Sciences|Sociology|Rural sociology"],
        })
        self.assertEqual(res.status_code, 201, res.content)
        doc = Document.objects.get(pk=res.json()["id"])
        self.assertEqual(
            sorted(t.subject for t in doc.topics.all()),
            ["Civil Engineering", "Sociology"])
        self.assertEqual(
            doc.topics.get(subject="Sociology").field, "Social Sciences")

    def test_a_tag_without_a_field_gets_one_derived(self):
        res = _client(self.owner).post(self.URL, {
            "title": "Notes", "file": self._pdf("a.pdf"),
            "topics": ["Physics|Optics"],
        })
        doc = Document.objects.get(pk=res.json()["id"])
        self.assertEqual(doc.topics.get(subject="Physics").field, "Sciences")

    def test_the_same_file_twice_in_one_set_is_not_stored_twice(self):
        """A slip, not an upload. Refusing the whole request would discard
        the other files the member correctly picked."""
        res = _client(self.owner).post(self.URL, {
            "title": "Dupes",
            "file": self._pdf("a.pdf", b"%PDF-1.4 same"),
            "files": [self._pdf("b.pdf", b"%PDF-1.4 same")],
        })
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(
            Document.objects.get(pk=res.json()["id"]).files.count(), 1)

    def test_a_disguised_file_is_refused_by_name(self):
        """⚠ "This file's content doesn't match its extension" is useless
        when eleven files were submitted at once."""
        res = _client(self.owner).post(self.URL, {
            "title": "Sneaky",
            "file": self._pdf("ok.pdf"),
            "files": [self._pdf("evil.pdf", b"<html><script>x</script>")],
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("evil.pdf", str(res.json()))

    def test_a_set_is_capped(self):
        res = _client(self.owner).post(self.URL, {
            "title": "Too many",
            "file": self._pdf("a.pdf"),
            "files": [self._pdf(f"f{i}.pdf", b"%PDF-1.4 " + str(i).encode())
                      for i in range(21)],
        })
        self.assertEqual(res.status_code, 400)

    def test_the_card_reports_every_file(self):
        """The design's card reads "2 files: DOCX, XLSX" — which needs the
        file list on the wire, not just a count."""
        res = _client(self.owner).post(self.URL, {
            "title": "Two files",
            "file": self._pdf("a.pdf", b"%PDF-1.4 one"),
            "files": [self._pdf("b.pdf", b"%PDF-1.4 two")],
        })
        card = _client().get(f"{self.URL}{res.json()['id']}/").json()["doc"]
        self.assertEqual(len(card["files"]), 2)
        self.assertEqual(
            sorted(f["name"] for f in card["files"]), ["a.pdf", "b.pdf"])

    def test_the_detail_page_gets_its_verification_panels(self):
        res = _client(self.owner).post(self.URL, {
            "title": "Panelled", "subject": "Physics",
            "file": self._pdf("a.pdf"),
        })
        body = _client().get(f"{self.URL}{res.json()['id']}/").json()["doc"]
        detail = body["verification_detail"]
        self.assertEqual(detail["heading"], "Not checked yet")
        self.assertIn("Physics", detail["sentence"])
        self.assertEqual(len(detail["history"]), 1)

    def test_the_panels_are_absent_while_the_flag_is_off(self):
        res = _client(self.owner).post(self.URL, {
            "title": "Unpanelled", "file": self._pdf("a.pdf")})
        gs = GlobalSettings.load()
        gs.explore_verification_enabled = False
        gs.save()
        body = _client().get(f"{self.URL}{res.json()['id']}/").json()["doc"]
        self.assertIsNone(body["verification_detail"])

    def test_a_video_is_now_accepted(self):
        """The 200 MB / all-types change. Before this, .mp4 was refused."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        res = _client(self.owner).post(self.URL, {
            "title": "Total station survey",
            "file": SimpleUploadedFile(
                "demo.mp4", b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32,
                content_type="video/mp4"),
        })
        self.assertEqual(res.status_code, 201, res.content)


class EngineWiringTest(VerificationApiTestCase):
    """⚠ THE POINT OF THIS CLASS.

    `invite_reviewers`, `expire_stale_invites`, `sync_teacher_specializations`
    and `bump_version` were all written, unit-tested and **never called**. Six
    admin settings, two of the six transition rules and two of the four
    notification kinds were configurable, described in the admin's own summary
    box, and completely inert — and every test passed, because each test
    called the function directly.

    These tests assert the CALLERS, not the functions. A test that invokes
    `invite_reviewers()` proves nothing about whether uploading invites anyone.
    """

    URL = "/api/explore/documents/"

    def _pdf(self, name="a.pdf", body=b"%PDF-1.4 x"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(name, body, content_type="application/pdf")

    def test_uploading_a_set_invites_reviewers(self):
        """The default policy is "Invite automatically" and the admin summary
        promises "The 3 least busy reviewers in its subjects are invited"."""
        self._specialist("wire1", "Physics")
        self._specialist("wire2", "Physics")
        res = _client(self.owner).post(self.URL, {
            "title": "Fresh", "subject": "Physics", "topic": "Optics",
            "file": self._pdf(),
        })
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(
            VerificationInvite.objects.filter(
                document_id=res.json()["id"], state=InviteState.OPEN).count(), 2)

    def test_the_invite_carries_a_deadline_from_the_setting(self):
        self.vsettings.days_to_respond = 3
        self.vsettings.save()
        self._specialist("wire3", "Physics")
        res = _client(self.owner).post(self.URL, {
            "title": "Deadline", "subject": "Physics", "file": self._pdf()})
        invite = VerificationInvite.objects.get(document_id=res.json()["id"])
        self.assertEqual((invite.expires_at - timezone.now()).days, 2)

    def test_the_open_queue_policy_invites_nobody(self):
        self.vsettings.reviewer_selection = "open_queue"
        self.vsettings.save()
        self._specialist("wire4", "Physics")
        res = _client(self.owner).post(self.URL, {
            "title": "Open", "subject": "Physics", "file": self._pdf()})
        self.assertEqual(
            VerificationInvite.objects.filter(
                document_id=res.json()["id"]).count(), 0)

    def test_nothing_is_invited_while_the_feature_is_off(self):
        gs = GlobalSettings.load()
        gs.explore_verification_enabled = False
        gs.save()
        self._specialist("wire5", "Physics")
        res = _client(self.owner).post(self.URL, {
            "title": "Dark", "subject": "Physics", "file": self._pdf()})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(VerificationInvite.objects.count(), 0)

    def test_an_upload_still_succeeds_if_matching_blows_up(self):
        """⚠ Nobody should lose an upload because the matching query raised."""
        from unittest.mock import patch
        self._specialist("wire6", "Physics")
        with patch("documents.verification.invite_reviewers",
                   side_effect=RuntimeError("boom")):
            res = _client(self.owner).post(self.URL, {
                "title": "Resilient", "subject": "Physics", "file": self._pdf()})
        self.assertEqual(res.status_code, 201, res.content)

    def test_the_expiry_task_runs_the_sweep(self):
        from documents.tasks import expire_verification_invites
        doc = self._set("Physics")
        reviewer = self._specialist("wire7", "Physics")
        VerificationInvite.objects.create(
            document=doc, version=1, reviewer=reviewer, subject="Physics",
            state=InviteState.OPEN,
            expires_at=timezone.now() - timezone.timedelta(days=1))
        self.assertEqual(expire_verification_invites()["expired"], 1)

    def test_the_expiry_task_is_a_no_op_while_the_feature_is_off(self):
        from documents.tasks import expire_verification_invites
        gs = GlobalSettings.load()
        gs.explore_verification_enabled = False
        gs.save()
        self.assertIn("skipped", expire_verification_invites())

    def test_the_reminder_task_sends_once_and_records_it(self):
        from documents.tasks import send_verification_reminders
        self.vsettings.reminder_days_before = 2
        self.vsettings.save()
        doc = self._set("Physics")
        reviewer = self._specialist("wire8", "Physics")
        invite = VerificationInvite.objects.create(
            document=doc, version=1, reviewer=reviewer, subject="Physics",
            state=InviteState.OPEN,
            expires_at=timezone.now() + timezone.timedelta(hours=20))
        self.assertEqual(send_verification_reminders()["reminded"], 1)
        invite.refresh_from_db()
        self.assertIsNotNone(invite.reminder_sent_at)
        self.assertEqual(send_verification_reminders()["reminded"], 0)

    def test_marking_notifications_read_does_not_forge_a_reminder(self):
        """⚠ THE POINT. "Mark all as read" used to stamp `reminder_sent_at`,
        which the queue row renders as "· reminder sent". Clicking it made the
        UI claim a reminder had been sent that never was — and suppressed the
        real one the sweep would have sent."""
        doc = self._set("Physics")
        reviewer = self._specialist("wire9", "Physics")
        invite = VerificationInvite.objects.create(
            document=doc, version=1, reviewer=reviewer, subject="Physics",
            state=InviteState.OPEN,
            expires_at=timezone.now() + timezone.timedelta(days=5))
        _client(reviewer).post(f"{BASE}/notifications/read/", {}, format="json")
        invite.refresh_from_db()
        self.assertIsNone(invite.reminder_sent_at)
        self.assertIsNotNone(invite.read_at)

    def test_staffing_a_subject_confirms_the_teacher(self):
        """The design: "Teachers review in the subjects they teach, with no
        extra step." """
        from courses.models import (
            Course, Subject, TeachingAssignment,
        )
        teacher = _user("wire_teacher")
        course = Course.objects.create(title="Class 10", slug="class-10-wire")
        subject = Subject.objects.create(course=course, name="Physics")
        TeachingAssignment.objects.create(
            subject=subject, teacher=teacher, is_active=True)
        self.assertTrue(
            SubjectSpecialization.objects.filter(
                user=teacher, subject="Physics",
                status=SpecializationStatus.CONFIRMED,
                source=SpecializationSource.TEACHER).exists())

    def test_an_assignment_with_no_teacher_confirms_nobody(self):
        """⚠ TeachingAssignment.teacher is SET_NULL and prod carries ACTIVE
        PRIMARY rows with nobody in them."""
        from courses.models import Course, Subject, TeachingAssignment
        course = Course.objects.create(title="Class 9", slug="class-9-wire")
        subject = Subject.objects.create(course=course, name="Chemistry")
        TeachingAssignment.objects.create(
            subject=subject, teacher=None, is_active=True)
        self.assertFalse(
            SubjectSpecialization.objects.filter(subject="Chemistry").exists())

    def test_the_teacher_backstop_task_catches_bulk_updates(self):
        from documents.tasks import sync_teacher_reviewers
        from courses.models import Course, Subject, TeachingAssignment
        gs = GlobalSettings.load()
        gs.explore_verification_enabled = False
        gs.save()
        teacher = _user("wire_bulk")
        course = Course.objects.create(title="Class 8", slug="class-8-wire")
        subject = Subject.objects.create(course=course, name="Biology")
        TeachingAssignment.objects.create(
            subject=subject, teacher=teacher, is_active=True)
        self.assertFalse(SubjectSpecialization.objects.filter(user=teacher).exists())
        gs.explore_verification_enabled = True
        gs.save()
        self.assertEqual(sync_teacher_reviewers()["confirmed"], 1)


class NewVersionTest(VerificationApiTestCase):
    """⚠ `bump_version()` was reachable only from the admin restore action.
    The member-facing "Upload a new version" button navigated to the generic
    upload form, which created a SECOND, SEPARATE document — leaving the
    rejected set frozen forever with nobody able to act on it."""

    def _pdf(self, name="v.pdf", body=b"%PDF-1.4 x"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(name, body, content_type="application/pdf")

    def _rejected(self):
        doc = self._set("Physics", title="Needs work")
        v.record_review(
            self._specialist("nv_rev", "Physics"), doc, matches_tags=True,
            content_accurate=False, files_complete=True, note="Fix page 3.")
        doc.refresh_from_db()
        return doc

    def test_a_new_version_restarts_the_count_on_the_same_set(self):
        doc = self._rejected()
        res = _client(self.owner).post(
            f"{BASE}/sets/{doc.id}/version/",
            {"files": [self._pdf("fixed.pdf", b"%PDF-1.4 fixed")]})
        self.assertEqual(res.status_code, 201, res.content)
        doc.refresh_from_db()
        self.assertEqual(doc.verification_version, 2)
        self.assertEqual(doc.verification_status, VerificationStatus.UNVERIFIED)
        self.assertEqual(v.change_request_count(doc), 0)
        self.assertEqual(Document.objects.filter(title="Needs work").count(), 1)

    def test_it_replaces_the_files_but_keeps_the_history(self):
        doc = self._rejected()
        _client(self.owner).post(
            f"{BASE}/sets/{doc.id}/version/",
            {"files": [self._pdf("fixed.pdf", b"%PDF-1.4 fixed")]})
        self.assertEqual(doc.files.count(), 1)
        self.assertEqual(doc.files.get().original_name, "fixed.pdf")
        self.assertEqual(
            VerificationReview.objects.filter(document=doc).count(), 1)

    def test_only_the_uploader_may_post_one(self):
        doc = self._rejected()
        res = _client(_user("nv_stranger")).post(
            f"{BASE}/sets/{doc.id}/version/", {"files": [self._pdf()]})
        self.assertEqual(res.status_code, 403)

    def test_a_disguised_file_is_refused(self):
        doc = self._rejected()
        res = _client(self.owner).post(
            f"{BASE}/sets/{doc.id}/version/",
            {"files": [self._pdf("evil.pdf", b"<html><script>x</script>")]})
        self.assertEqual(res.status_code, 400)

    def test_it_needs_at_least_one_file(self):
        doc = self._rejected()
        res = _client(self.owner).post(f"{BASE}/sets/{doc.id}/version/", {})
        self.assertEqual(res.status_code, 400)

    def test_the_earlier_reviewer_may_look_again(self):
        doc = self._rejected()
        reviewer = SubjectSpecialization.objects.get(subject="Physics").user
        _client(self.owner).post(
            f"{BASE}/sets/{doc.id}/version/",
            {"files": [self._pdf("fixed.pdf", b"%PDF-1.4 fixed")]})
        doc.refresh_from_db()
        self.assertTrue(v.can_review(reviewer, doc)[0])


class BrowsePanelTest(VerificationApiTestCase):
    """§6 and §9 — the three-level browse panel and the file-type row.

    The tree is served from `/facets/` with a count on every node. What is
    worth testing is the counting rule and the narrowing behaviour, both of
    which are silently wrong in ways a screenshot would not reveal.
    """

    FACETS = "/api/explore/facets/"
    LIST = "/api/explore/documents/"

    def _tree(self):
        return _client().get(self.FACETS).json()["fieldGroups"]

    def _field(self, name):
        return next(f for f in self._tree() if f["field"] == name)

    def test_the_tree_has_three_levels(self):
        self._set("Civil Engineering", title="Dams")
        eng = self._field("Engineering")
        civil = next(s for s in eng["subjects"] if s["subject"] == "Civil Engineering")
        self.assertIn("branches", civil)
        self.assertIn("count", civil)

    def test_every_field_renders_even_when_empty(self):
        """The design shows a count on every chip including zero. A field that
        vanishes with its last document makes the panel change shape between
        page loads."""
        names = [f["field"] for f in self._tree()]
        for expected in ("Engineering", "Social Sciences", "Sciences",
                         "Humanities", "Commerce", "Exam Prep"):
            self.assertIn(expected, names)

    def test_counts_are_of_documents_not_of_tag_rows(self):
        """⚠ THE POINT. A set tagged under two branches of the SAME subject
        is one document. Counting tag rows would show it twice on a chip
        people use to decide whether a branch is worth opening."""
        doc = Document.objects.create(owner=self.owner, title="Two branches")
        doc.topics.all().delete()
        for i, branch in enumerate(("Structural", "Geotechnical")):
            DocumentTopic.objects.create(
                document=doc, field="Engineering",
                subject="Civil Engineering", branch=branch, order=i)
        civil = next(s for s in self._field("Engineering")["subjects"]
                     if s["subject"] == "Civil Engineering")
        self.assertEqual(civil["count"], 1)

    def test_a_hidden_set_is_not_counted(self):
        """The tree is built from what the viewer can actually see."""
        doc = self._set("Civil Engineering", title="Hidden")
        doc.verification_status = VerificationStatus.HIDDEN
        doc.save(update_fields=["verification_status"])
        self.assertEqual(self._field("Engineering")["count"], 0)

    def test_filtering_by_field(self):
        self._set("Civil Engineering", title="Eng one")
        self._set("Sociology", title="Soc one")
        titles = [d["title"] for d in
                  _client().get(self.LIST, {"field": "Engineering"}).json()["results"]]
        self.assertEqual(titles, ["Eng one"])

    def test_either_tag_path_finds_a_multi_tagged_set(self):
        """⚠ The whole reason tags are a table. A set tagged under Civil
        Engineering AND Sociology must appear under both."""
        self._set("Civil Engineering", "Sociology", title="Dams")
        for field in ("Engineering", "Social Sciences"):
            titles = [d["title"] for d in
                      _client().get(self.LIST, {"field": field}).json()["results"]]
            self.assertEqual(titles, ["Dams"], field)

    def test_filtering_by_branch(self):
        doc = self._set("Civil Engineering", title="Water")
        doc.topics.update(branch="Water resources")
        self._set("Civil Engineering", title="Other")
        titles = [d["title"] for d in
                  _client().get(self.LIST, {"branch": "Water resources"}).json()["results"]]
        self.assertEqual(titles, ["Water"])

    def test_the_file_type_row_matches_families_not_extensions(self):
        """The design's row is Documents / Slides / Data / Images / Audio and
        video / Other, and it reads the set's FILES — so a set holding a PDF
        and an XLSX answers to both Documents and Data."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import DocumentFile
        doc = self._set("Physics", title="Mixed")
        DocumentFile.objects.create(
            document=doc, owner_id=self.owner.id,
            file=SimpleUploadedFile("a.pdf", b"%PDF-1.4 a"),
            original_name="a.pdf", filetype="PDF", file_hash="fk1")
        DocumentFile.objects.create(
            document=doc, owner_id=self.owner.id,
            file=SimpleUploadedFile("b.xlsx", b"PK\x03\x04b"),
            original_name="b.xlsx", filetype="XLSX", file_hash="fk2")
        for kind in ("documents", "data"):
            titles = [d["title"] for d in
                      _client().get(self.LIST, {"filekind": kind}).json()["results"]]
            self.assertIn("Mixed", titles, kind)
        titles = [d["title"] for d in
                  _client().get(self.LIST, {"filekind": "images"}).json()["results"]]
        self.assertNotIn("Mixed", titles)

    def test_a_set_appears_once_however_many_files_match(self):
        """A join across `files` duplicates the parent per matching row."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import DocumentFile
        doc = self._set("Physics", title="Three PDFs")
        for i in range(3):
            DocumentFile.objects.create(
                document=doc, owner_id=self.owner.id,
                file=SimpleUploadedFile(f"f{i}.pdf", b"%PDF-1.4 " + str(i).encode()),
                original_name=f"f{i}.pdf", filetype="PDF", file_hash=f"many{i}")
        titles = [d["title"] for d in
                  _client().get(self.LIST, {"filekind": "documents"}).json()["results"]]
        self.assertEqual(titles.count("Three PDFs"), 1)

    def test_an_unknown_filekind_does_not_400(self):
        self._set("Physics", title="Anything")
        res = _client().get(self.LIST, {"filekind": "banana"})
        self.assertEqual(res.status_code, 200)
