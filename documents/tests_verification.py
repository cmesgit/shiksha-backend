"""Phases 2–3 — the community verification rules.

Every assertion here maps to a line of the design's "How it works" tab. They
are written against `documents/verification.py` rather than through the API on
purpose: these are the rules, and a view that re-derives one of them is the bug
this module exists to prevent.
"""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from . import verification as v
from .models import (
    Document,
    DocumentTopic,
    InviteState,
    ReviewOutcome,
    ReviewerSelection,
    SpecializationSource,
    SpecializationStatus,
    SubjectSpecialization,
    VerificationInvite,
    VerificationReview,
    VerificationSettings,
    VerificationStatus,
)


def _user(name):
    return get_user_model().objects.create_user(
        username=name, email=f"{name}@example.com", password="pw12345!",
    )


def _specialist(name, *subjects, status=SpecializationStatus.CONFIRMED):
    user = _user(name)
    for subject in subjects:
        SubjectSpecialization.objects.create(
            user=user, subject=subject, status=status,
            source=SpecializationSource.APPROVED,
        )
    return user


class VerificationTestCase(TestCase):
    def setUp(self):
        self.settings = VerificationSettings.load()
        self.owner = _user("vowner")

    def _set(self, *tags, owner=None, title="A set", **kw):
        """A set tagged with one `subject/branch` pair per tag tuple."""
        doc = Document.objects.create(
            owner=owner or self.owner, title=title, **kw)
        # The post_save signal writes a topic from Document.subject; these
        # tests tag explicitly, so start from a clean slate.
        doc.topics.all().delete()
        for i, tag in enumerate(tags):
            subject, branch = tag if isinstance(tag, tuple) else (tag, "")
            DocumentTopic.objects.create(
                document=doc, subject=subject, branch=branch,
                field=v.field_for_subject(subject), order=i)
        return doc

    def _confirm(self, doc, *users):
        for user in users:
            v.record_review(
                user, doc, matches_tags=True, content_accurate=True,
                files_complete=True)
            doc.refresh_from_db()
        return doc


class EligibilityTest(VerificationTestCase):
    def test_a_confirmed_specialist_in_any_tag_may_review(self):
        """⚠ ANY tag, not the first. The design's own sample set is tagged
        under both Civil Engineering and Sociology, and either one qualifies
        a reviewer. Code that asks about "the" subject has already lost the
        feature."""
        doc = self._set("Civil Engineering", "Sociology")
        sociologist = _specialist("soc1", "Sociology")
        self.assertTrue(v.is_specialist(sociologist, doc))
        self.assertTrue(v.can_review(sociologist, doc)[0])

    def test_a_specialist_in_an_untagged_subject_may_not(self):
        doc = self._set("Civil Engineering")
        self.assertFalse(v.can_review(_specialist("hist1", "History"), doc)[0])

    def test_a_pending_specialization_does_not_count(self):
        """PENDING means somebody typed a subject into a box. If it counted,
        the admin's approve/decline would be decoration."""
        doc = self._set("Physics")
        pending = _specialist("pend1", "Physics",
                              status=SpecializationStatus.PENDING)
        self.assertFalse(v.is_specialist(pending, doc))

    def test_a_declined_specialization_does_not_count(self):
        doc = self._set("Physics")
        declined = _specialist("dec1", "Physics",
                               status=SpecializationStatus.DECLINED)
        self.assertFalse(v.is_specialist(declined, doc))

    def test_matching_ignores_case_and_padding(self):
        """`subject` is free text on both the tag and the specialization."""
        doc = self._set("  civil engineering ")
        self.assertTrue(v.is_specialist(_specialist("c1", "Civil Engineering"), doc))

    def test_nobody_reviews_their_own_upload(self):
        """Written on the How it works tab, and the prototype did NOT enforce
        it — a member could confirm their own set. That makes the badge
        self-issued."""
        doc = self._set("Physics")
        SubjectSpecialization.objects.create(
            user=self.owner, subject="Physics",
            status=SpecializationStatus.CONFIRMED)
        allowed, reason = v.can_review(self.owner, doc)
        self.assertFalse(allowed)
        self.assertIn("your own", reason)

    def test_an_anonymous_visitor_may_not_review(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(v.can_review(AnonymousUser(), self._set("Physics"))[0])


class ThresholdTest(VerificationTestCase):
    def test_three_confirmations_verify_a_set(self):
        doc = self._set("Physics")
        reviewers = [_specialist(f"th{i}", "Physics") for i in range(3)]
        self._confirm(doc, *reviewers[:2])
        self.assertEqual(doc.verification_status, VerificationStatus.UNVERIFIED)
        self.assertEqual(v.confirmation_count(doc), 2)
        self._confirm(doc, reviewers[2])
        self.assertEqual(doc.verification_status, VerificationStatus.VERIFIED)
        self.assertIsNotNone(doc.verified_at)

    def test_the_threshold_follows_the_setting(self):
        """The admin's "Confirmations needed" stepper, not a constant."""
        self.settings.confirmations_needed = 1
        self.settings.save()
        doc = self._set("Physics")
        self._confirm(doc, _specialist("one1", "Physics"))
        self.assertEqual(doc.verification_status, VerificationStatus.VERIFIED)

    def test_a_member_reviews_a_version_once(self):
        doc = self._set("Physics")
        reviewer = _specialist("once1", "Physics")
        self._confirm(doc, reviewer)
        allowed, reason = v.can_review(reviewer, doc)
        self.assertFalse(allowed)
        self.assertIn("already reviewed", reason)

    def test_the_database_enforces_it_too(self):
        """⚠ A view-level check races. Two submits from a double-clicked
        button both read zero reviews and both write, and the set gets two
        confirmations from one person."""
        doc = self._set("Physics")
        reviewer = _specialist("race1", "Physics")
        VerificationReview.objects.create(
            document=doc, version=doc.verification_version, reviewer=reviewer,
            matches_tags=True, content_accurate=True, files_complete=True,
            outcome=ReviewOutcome.CONFIRMED)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VerificationReview.objects.create(
                    document=doc, version=doc.verification_version,
                    reviewer=reviewer, matches_tags=True,
                    content_accurate=True, files_complete=True,
                    outcome=ReviewOutcome.CONFIRMED)


class ChangesRequestedTest(VerificationTestCase):
    def test_one_no_pauses_the_count_but_keeps_the_set_in_browse(self):
        """⚠ CHANGES_REQUESTED is not a takedown. If one reviewer's "no" hid
        a set, any specialist could remove anyone's upload from the library
        single-handed."""
        doc = self._set("Physics")
        self._confirm(doc, _specialist("cr1", "Physics"))
        v.record_review(
            _specialist("cr2", "Physics"), doc, matches_tags=True,
            content_accurate=False, files_complete=True,
            note="Page 3 contradicts page 2.")
        doc.refresh_from_db()
        self.assertEqual(doc.verification_status,
                         VerificationStatus.CHANGES_REQUESTED)
        self.assertEqual(v.confirmation_count(doc), 1)
        self.assertFalse(doc.is_removed)

    def test_a_no_requires_a_note(self):
        """The design relabels the field "(required)" and the button
        "Request changes" together. A change request with no note tells the
        uploader nothing actionable."""
        doc = self._set("Physics")
        with self.assertRaises(ValueError):
            v.record_review(
                _specialist("cr3", "Physics"), doc, matches_tags=True,
                content_accurate=False, files_complete=True, note="   ")

    def test_a_confirmation_does_not_require_a_note(self):
        doc = self._set("Physics")
        v.record_review(_specialist("cr4", "Physics"), doc, matches_tags=True,
                        content_accurate=True, files_complete=True)
        self.assertEqual(v.confirmation_count(doc), 1)

    def test_two_change_requests_hide_the_set(self):
        doc = self._set("Physics")
        for i in range(2):
            v.record_review(
                _specialist(f"hid{i}", "Physics"), doc, matches_tags=False,
                content_accurate=True, files_complete=True, note="Wrong tags.")
        doc.refresh_from_db()
        self.assertEqual(doc.verification_status, VerificationStatus.HIDDEN)

    def test_the_hide_threshold_follows_the_setting(self):
        self.settings.change_requests_before_hidden = 3
        self.settings.save()
        doc = self._set("Physics")
        for i in range(2):
            v.record_review(
                _specialist(f"hid3{i}", "Physics"), doc, matches_tags=False,
                content_accurate=True, files_complete=True, note="No.")
        doc.refresh_from_db()
        self.assertEqual(doc.verification_status,
                         VerificationStatus.CHANGES_REQUESTED)

    def test_a_hidden_set_cannot_be_reviewed_further(self):
        doc = self._set("Physics")
        for i in range(2):
            v.record_review(
                _specialist(f"hf{i}", "Physics"), doc, matches_tags=False,
                content_accurate=True, files_complete=True, note="No.")
        doc.refresh_from_db()
        self.assertFalse(v.can_review(_specialist("hf9", "Physics"), doc)[0])


class NewVersionTest(VerificationTestCase):
    def test_a_new_version_restarts_the_count(self):
        doc = self._set("Physics")
        self._confirm(doc, *[_specialist(f"nv{i}", "Physics") for i in range(2)])
        self.assertEqual(v.confirmation_count(doc), 2)
        v.bump_version(doc)
        doc.refresh_from_db()
        self.assertEqual(v.confirmation_count(doc), 0)
        self.assertEqual(doc.verification_version, 2)

    def test_it_does_not_delete_the_history(self):
        """⚠ The reviews stop COUNTING, they do not stop EXISTING. The
        document page's history panel is supposed to show that someone asked
        for changes and the uploader fixed it."""
        doc = self._set("Physics")
        self._confirm(doc, _specialist("nv3", "Physics"))
        v.bump_version(doc)
        self.assertEqual(VerificationReview.objects.filter(document=doc).count(), 1)

    def test_editing_a_verified_set_returns_it_to_unverified(self):
        doc = self._set("Physics")
        self._confirm(doc, *[_specialist(f"nv4{i}", "Physics") for i in range(3)])
        self.assertEqual(doc.verification_status, VerificationStatus.VERIFIED)
        v.bump_version(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.verification_status, VerificationStatus.UNVERIFIED)
        self.assertIsNone(doc.verified_at)

    def test_an_earlier_reviewer_may_review_the_corrected_version(self):
        """The whole point of asking for changes: the same specialist looks
        again. "Each member reviews a VERSION once", not "a set once"."""
        doc = self._set("Physics")
        reviewer = _specialist("nv5", "Physics")
        v.record_review(reviewer, doc, matches_tags=True,
                        content_accurate=False, files_complete=True,
                        note="Fix page 3.")
        doc.refresh_from_db()
        v.bump_version(doc)
        doc.refresh_from_db()
        self.assertTrue(v.can_review(reviewer, doc)[0])

    def test_it_supersedes_the_open_invites(self):
        """An invite to review version 1 is not "missed" when version 2
        arrives — it stopped being answerable."""
        doc = self._set("Physics")
        _specialist("nv6", "Physics")
        v.invite_reviewers(doc)
        v.bump_version(doc)
        self.assertEqual(
            VerificationInvite.objects.filter(
                document=doc, state=InviteState.SUPERSEDED).count(), 1)


class InviteTest(VerificationTestCase):
    def test_automatic_invites_go_to_the_least_busy_first(self):
        busy = _specialist("inv_busy", "Physics")
        idle = _specialist("inv_idle", "Physics")
        other = self._set("Physics", owner=_user("other_owner"))
        VerificationInvite.objects.create(
            document=other, version=1, reviewer=busy, state=InviteState.OPEN)

        self.settings.reviewers_invited_per_set = 1
        self.settings.save()
        doc = self._set("Physics")
        created = v.invite_reviewers(doc)
        self.assertEqual([i.reviewer_id for i in created], [idle.id])

    def test_a_reviewer_at_their_limit_is_not_invited(self):
        self.settings.open_reviews_per_reviewer = 1
        self.settings.save()
        maxed = _specialist("inv_max", "Physics")
        other = self._set("Physics", owner=_user("other_owner2"))
        VerificationInvite.objects.create(
            document=other, version=1, reviewer=maxed, state=InviteState.OPEN)
        self.assertEqual(v.eligible_reviewers(self._set("Physics")), [])

    def test_the_uploader_is_never_invited(self):
        SubjectSpecialization.objects.create(
            user=self.owner, subject="Physics",
            status=SpecializationStatus.CONFIRMED)
        self.assertEqual(v.eligible_reviewers(self._set("Physics")), [])

    def test_a_paused_reviewer_is_not_invited(self):
        spec = SubjectSpecialization.objects.create(
            user=_user("inv_paused"), subject="Physics",
            status=SpecializationStatus.CONFIRMED, is_paused=True)
        self.assertEqual(v.eligible_reviewers(self._set("Physics")), [])
        spec.is_paused = False
        spec.save()
        self.assertEqual(len(v.eligible_reviewers(self._set("Physics", title="B"))), 1)

    def test_colleagues_of_the_uploader_are_skipped(self):
        """The design: "This also leaves out their colleagues and
        classmates."""
        colleague = _specialist("inv_coll", "Physics")
        Document.objects.create(
            owner=colleague, title="Their own doc", institution="NIT Silchar")
        doc = self._set("Physics", institution="NIT Silchar")
        self.assertEqual(v.eligible_reviewers(doc), [])

    def test_no_invites_under_the_open_queue_policy(self):
        """The design: "Open queue" drops the invite deadlines entirely — the set is just
        in everyone's queue."""
        self.settings.reviewer_selection = ReviewerSelection.OPEN_QUEUE
        self.settings.save()
        _specialist("inv_open", "Physics")
        self.assertEqual(v.invite_reviewers(self._set("Physics")), [])

    def test_inviting_twice_tops_up_rather_than_duplicating(self):
        for i in range(4):
            _specialist(f"inv_dup{i}", "Physics")
        doc = self._set("Physics")
        first = v.invite_reviewers(doc)
        self.assertEqual(len(first), 3)
        self.assertEqual(v.invite_reviewers(doc), [])

    def test_widening_to_the_field_only_happens_when_nobody_matches(self):
        """The design: "If nobody is available, reviewers from the same field
        are invited." A sibling-subject specialist must not displace a real
        one."""
        _specialist("wide_exact", "Civil Engineering")
        _specialist("wide_sibling", "Mechanical Engineering")
        doc = self._set("Civil Engineering")
        found = v.eligible_reviewers(doc)
        self.assertEqual([s.subject for s in found], ["Civil Engineering"])

    def test_widening_reaches_the_sibling_when_there_is_no_one_else(self):
        sibling = _specialist("wide_only", "Mechanical Engineering")
        doc = self._set("Civil Engineering")
        self.assertEqual([s.user_id for s in v.eligible_reviewers(doc)],
                         [sibling.id])

    def test_widening_can_be_turned_off(self):
        self.settings.widen_to_same_field = False
        self.settings.save()
        _specialist("wide_off", "Mechanical Engineering")
        self.assertEqual(v.eligible_reviewers(self._set("Civil Engineering")), [])


class ExpiryTest(VerificationTestCase):
    def _expired_invite(self, reviewer, doc, **kw):
        return VerificationInvite.objects.create(
            document=doc, version=doc.verification_version, reviewer=reviewer,
            subject="Physics", state=InviteState.OPEN,
            expires_at=timezone.now() - timezone.timedelta(days=1), **kw)

    def test_an_unanswered_invite_expires(self):
        doc = self._set("Physics")
        self._expired_invite(_specialist("exp1", "Physics"), doc)
        expired, _ = v.expire_stale_invites()
        self.assertEqual(len(expired), 1)
        self.assertEqual(
            VerificationInvite.objects.get(pk=expired[0].pk).state,
            InviteState.EXPIRED)

    def test_a_reviewer_who_keeps_missing_them_is_paused(self):
        self.settings.missed_invites_before_pause = 2
        self.settings.save()
        reviewer = _specialist("exp2", "Physics")
        for i in range(2):
            doc = self._set("Physics", title=f"Set {i}",
                            owner=_user(f"exp_owner{i}"))
            self._expired_invite(reviewer, doc)
            v.expire_stale_invites()
        self.assertTrue(
            SubjectSpecialization.objects.get(
                user=reviewer, subject="Physics").is_paused)

    def test_pausing_can_be_turned_off(self):
        self.settings.pause_inactive_reviewers = False
        self.settings.missed_invites_before_pause = 1
        self.settings.save()
        reviewer = _specialist("exp3", "Physics")
        self._expired_invite(reviewer, self._set("Physics"))
        v.expire_stale_invites()
        self.assertFalse(
            SubjectSpecialization.objects.get(
                user=reviewer, subject="Physics").is_paused)

    def test_an_admin_assignment_is_not_rotated_away(self):
        """⚠ An admin reached the Needs attention tab precisely because the
        automatic flow failed. Letting the same sweep expire their manual fix
        puts the set straight back on the list it just left."""
        doc = self._set("Physics")
        self._expired_invite(_specialist("exp4", "Physics"), doc,
                             assigned_by_admin=True)
        expired, _ = v.expire_stale_invites()
        self.assertEqual(expired, [])

    def test_expiry_passes_the_set_to_the_next_reviewer(self):
        doc = self._set("Physics")
        first = _specialist("exp5", "Physics")
        _specialist("exp6", "Physics")
        self._expired_invite(first, doc)
        v.expire_stale_invites()
        self.assertTrue(
            VerificationInvite.objects.filter(
                document=doc, state=InviteState.OPEN).exists())


class EarningSpecializationTest(VerificationTestCase):
    def _verify(self, owner, subject, title):
        doc = self._set(subject, owner=owner, title=title)
        reviewers = [_specialist(f"es_{title}_{i}", subject) for i in range(3)]
        self._confirm(doc, *reviewers)
        return doc

    def test_two_verified_uploads_earn_the_subject(self):
        author = _user("earner")
        self._verify(author, "Physics", "a")
        self.assertFalse(
            SubjectSpecialization.objects.filter(
                user=author, subject="Physics",
                status=SpecializationStatus.CONFIRMED).exists())
        self._verify(author, "Physics", "b")
        self.assertTrue(
            SubjectSpecialization.objects.filter(
                user=author, subject="Physics",
                status=SpecializationStatus.CONFIRMED,
                source=SpecializationSource.EARNED).exists())

    def test_it_can_be_turned_off(self):
        self.settings.members_can_earn_it = False
        self.settings.save()
        author = _user("earner_off")
        self._verify(author, "Physics", "c")
        self._verify(author, "Physics", "d")
        self.assertFalse(
            SubjectSpecialization.objects.filter(user=author).exists())

    def test_earning_does_not_override_an_admin_decline(self):
        """⚠ THE POINT. If uploads could grant a subject an admin explicitly
        refused, the decline would mean nothing — and the member would never
        be told it had been reversed."""
        author = _user("earner_declined")
        SubjectSpecialization.objects.create(
            user=author, subject="Physics",
            status=SpecializationStatus.DECLINED)
        self._verify(author, "Physics", "e")
        self._verify(author, "Physics", "f")
        self.assertEqual(
            SubjectSpecialization.objects.get(
                user=author, subject="Physics").status,
            SpecializationStatus.DECLINED)

    def test_earning_resolves_a_pending_request(self):
        author = _user("earner_pending")
        SubjectSpecialization.objects.create(
            user=author, subject="Physics",
            status=SpecializationStatus.PENDING)
        self._verify(author, "Physics", "g")
        self._verify(author, "Physics", "h")
        self.assertEqual(
            SubjectSpecialization.objects.get(
                user=author, subject="Physics").status,
            SpecializationStatus.CONFIRMED)


class QueueTest(VerificationTestCase):
    def test_the_queue_holds_unverified_sets_in_my_subjects(self):
        reviewer = _specialist("q1", "Physics")
        mine = self._set("Physics", title="Mine")
        self._set("History", title="Not mine")
        self.assertEqual([d.id for d in v.review_queue(reviewer)], [mine.id])

    def test_longest_wait_first(self):
        reviewer = _specialist("q2", "Physics")
        first = self._set("Physics", title="Older")
        second = self._set("Physics", title="Newer")
        Document.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=9))
        self.assertEqual([d.title for d in v.review_queue(reviewer)],
                         ["Older", "Newer"])

    def test_my_own_uploads_are_not_in_my_queue(self):
        SubjectSpecialization.objects.create(
            user=self.owner, subject="Physics",
            status=SpecializationStatus.CONFIRMED)
        self._set("Physics")
        self.assertEqual(list(v.review_queue(self.owner)), [])

    def test_a_set_i_reviewed_leaves_my_queue(self):
        reviewer = _specialist("q3", "Physics")
        doc = self._set("Physics")
        self._confirm(doc, reviewer)
        self.assertEqual(list(v.review_queue(reviewer)), [])

    def test_but_its_corrected_version_comes_back(self):
        """⚠ THE POINT, and the reason this is an Exists() subquery rather
        than exclude(verification_reviews__reviewer=user). A plain exclude
        drops the document on ANY matching review row, so reviewing version 1
        would hide version 2 from the same specialist forever — exactly the
        person best placed to check the fix."""
        reviewer = _specialist("q4", "Physics")
        doc = self._set("Physics")
        v.record_review(reviewer, doc, matches_tags=True,
                        content_accurate=False, files_complete=True,
                        note="Fix page 3.")
        doc.refresh_from_db()
        self.assertEqual(list(v.review_queue(reviewer)), [])
        v.bump_version(doc)
        self.assertEqual([d.id for d in v.review_queue(reviewer)], [doc.id])

    def test_a_verified_set_leaves_everyones_queue(self):
        extra = _specialist("q5", "Physics")
        doc = self._set("Physics")
        self._confirm(doc, *[_specialist(f"q5r{i}", "Physics") for i in range(3)])
        self.assertEqual(list(v.review_queue(extra)), [])

    def test_a_removed_set_is_not_in_any_queue(self):
        reviewer = _specialist("q6", "Physics")
        doc = self._set("Physics")
        doc.is_removed = True
        doc.save(update_fields=["is_removed"])
        self.assertEqual(list(v.review_queue(reviewer)), [])

    def test_admin_assigns_limits_the_queue_to_assignments(self):
        self.settings.reviewer_selection = ReviewerSelection.ADMIN_ASSIGNS
        self.settings.save()
        reviewer = _specialist("q7", "Physics")
        unassigned = self._set("Physics", title="Not for them")
        assigned = self._set("Physics", title="Assigned")
        VerificationInvite.objects.create(
            document=assigned, version=1, reviewer=reviewer,
            state=InviteState.OPEN, assigned_by_admin=True)
        self.assertEqual([d.id for d in v.review_queue(reviewer)], [assigned.id])
        self.assertFalse(v.can_review(reviewer, unassigned)[0])

    def test_someone_with_no_specializations_has_no_queue(self):
        self._set("Physics")
        self.assertEqual(list(v.review_queue(_user("q8"))), [])


class NeedsAttentionTest(VerificationTestCase):
    def test_a_hidden_set_needs_attention(self):
        doc = self._set("Physics")
        doc.verification_status = VerificationStatus.HIDDEN
        doc.save(update_fields=["verification_status"])
        self.assertEqual(v.needs_admin_attention(doc), "hidden")

    def test_a_set_nobody_can_review_needs_attention(self):
        self.assertEqual(
            v.needs_admin_attention(self._set("Physics")), "no_reviewer")

    def test_expired_invites_with_nobody_left_need_attention(self):
        doc = self._set("Physics")
        reviewer = _specialist("na1", "Physics")
        VerificationInvite.objects.create(
            document=doc, version=1, reviewer=reviewer,
            state=InviteState.EXPIRED)
        self.assertEqual(v.needs_admin_attention(doc), "invites_expired")

    def test_a_set_with_a_live_invite_does_not(self):
        """The design: "Most sets never appear here." A list that fills up
        with ordinary in-progress work is a list nobody reads."""
        doc = self._set("Physics")
        _specialist("na2", "Physics")
        v.invite_reviewers(doc)
        self.assertEqual(v.needs_admin_attention(doc), "")

    def test_a_verified_set_never_does(self):
        doc = self._set("Physics")
        self._confirm(doc, *[_specialist(f"na3{i}", "Physics") for i in range(3)])
        self.assertEqual(v.needs_admin_attention(doc), "")


class SettingsTest(TestCase):
    def test_load_creates_the_row_with_the_designs_defaults(self):
        """The admin screen opens on these numbers and its summary box reads
        back the flow they describe — "the 3 least busy reviewers … 7 days …
        after 3 confirmations"."""
        s = VerificationSettings.load()
        self.assertEqual(s.confirmations_needed, 3)
        self.assertEqual(s.reviewers_invited_per_set, 3)
        self.assertEqual(s.days_to_respond, 7)
        self.assertEqual(s.change_requests_before_hidden, 2)
        self.assertEqual(s.open_reviews_per_reviewer, 5)
        self.assertEqual(s.verified_uploads_to_earn_it, 2)
        self.assertEqual(s.reminder_days_before, 2)
        self.assertEqual(s.missed_invites_before_pause, 3)
        self.assertEqual(s.reviewer_selection, ReviewerSelection.AUTOMATIC)
        self.assertTrue(s.show_unverified_in_browse)

    def test_it_stays_a_singleton(self):
        VerificationSettings.load()
        VerificationSettings(confirmations_needed=9).save()
        self.assertEqual(VerificationSettings.objects.count(), 1)
        self.assertEqual(VerificationSettings.load().confirmations_needed, 9)
