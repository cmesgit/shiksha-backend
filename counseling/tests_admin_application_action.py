"""AdminApplicationActionView — the approve/reject/suspend/relist guard.

WHAT BROKE, AND WHY THESE TESTS EXIST
-------------------------------------
The view used to do a bare `get_object_or_404(...)` with no status filter and
no row lock, then apply whatever action it was handed. Three live consequences:

  1. Two admins clicking Approve on the same applicant BOTH got a 200, and
     `notify(..., email=True)` ran twice — the applicant was emailed their
     approval twice. The queue screen is exactly where that happens, because
     two people work the same list seconds apart.
  2. Approve-then-Reject silently flipped a live, working counsellor to
     `rejected` with no error, taking them out of the directory and
     overwriting `review_note` / `reviewed_by` / `reviewed_at` with whoever
     clicked last.
  3. Rejecting never revoked the COUNSELOR role, so a rejected applicant kept
     their forum COUNSELOR badge (`forum/utils.py:39`).

The fix re-reads the row under `select_for_update()` inside the transaction
and refuses when the action does not apply to the row's CURRENT status. The
guard is deliberately PER ACTION, not a blanket "must be pending" — `relist`
on an already-approved profile is legitimate, so a blanket rule would have
broken it. The table is `AdminApplicationActionView.LEGAL_FROM`.

⚠ ON WHAT THESE TESTS CAN AND CANNOT PROVE
These drive the double-click SEQUENTIALLY, which is what the guard is for and
what the bug report described. They do NOT prove the row lock, because the
test settings run on SQLite where `select_for_update()` is a documented no-op.
What makes the sequential test meaningful anyway is that the guard is a
re-READ of committed state, not a flag cached from the first request: the
second caller re-queries and sees `approved`. The lock is what makes that
re-read correct under real concurrency on Postgres, and it cannot be asserted
here — do not "simplify" it away because no test fails when you do.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounts.models import Role, UserRole
from counseling.models import CounselorProfile
from counseling.views import AdminApplicationActionView
from notifications.models import Notification

User = get_user_model()


def url(profile_id):
    return f"/api/counseling/admin/applications/{profile_id}/action/"


class ActionTestBase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            email="aa-admin@example.com", username="aaadmin",
            password="x" * 14, is_staff=True)
        cls.other_admin = User.objects.create_user(
            email="aa-admin2@example.com", username="aaadmin2",
            password="x" * 14, is_staff=True)

    def setUp(self):
        super().setUp()
        self.applicant = User.objects.create_user(
            email="aa-applicant@example.com", username="aaapplicant",
            password="x" * 14)
        self.profile = CounselorProfile.objects.create(
            user=self.applicant, display_name="Dr. Meera Raut",
            status=CounselorProfile.STATUS_PENDING)
        self.client.force_authenticate(self.admin)

    # ── helpers ──────────────────────────────────────────────────────────
    def act(self, action, note="", as_user=None, profile_id=None):
        if as_user is not None:
            self.client.force_authenticate(as_user)
        return self.client.post(
            url(profile_id or self.profile.pk),
            {"action": action, "note": note}, format="json")

    def status_now(self):
        self.profile.refresh_from_db()
        return self.profile.status

    def notifications(self, verb):
        return Notification.objects.filter(recipient=self.applicant, verb=verb)

    def role_is_active(self):
        return UserRole.objects.filter(
            user=self.applicant, role__name="COUNSELOR", is_active=True
        ).exists()


class DoubleApproveTest(ActionTestBase):
    """Consequence 1: the applicant was emailed their approval twice."""

    def test_one_approve_notifies_once_and_grants_the_role(self):
        r = self.act("approve")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.status_now(), CounselorProfile.STATUS_APPROVED)
        self.assertTrue(self.profile.is_listed)
        self.assertTrue(self.role_is_active())
        self.assertEqual(self.notifications("counseling.approved").count(), 1)

    def test_a_second_approve_is_refused_and_does_not_notify_again(self):
        """THE headline case. A 200 here means someone got two emails."""
        self.assertEqual(self.act("approve").status_code, 200)

        r = self.act("approve", note="me too", as_user=self.other_admin)

        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("already approved", r.data["detail"])
        self.assertEqual(r.data["status"], CounselorProfile.STATUS_APPROVED)
        self.assertEqual(
            self.notifications("counseling.approved").count(), 1,
            "the approval notification fired twice — this is the bug")

    def test_the_refused_second_click_sends_no_second_EMAIL(self):
        """Asserted at the dispatch boundary as well as the Notification row,
        because the email is the part the applicant actually sees and it
        leaves the request through a different code path."""
        with patch("notifications.services._dispatch_email") as send:
            self.assertEqual(self.act("approve").status_code, 200)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(self.act("approve").status_code, 400)
            self.assertEqual(
                send.call_count, 1,
                "a refused action still emailed the applicant")

    def test_a_refused_action_does_not_touch_the_audit_fields(self):
        """`review_note`/`reviewed_by`/`reviewed_at` used to be overwritten by
        whoever clicked last, so the record no longer said who decided."""
        self.act("approve", note="verified credentials")
        self.profile.refresh_from_db()
        before = (self.profile.review_note, self.profile.reviewed_by_id,
                  self.profile.reviewed_at)

        self.act("approve", note="clobbered", as_user=self.other_admin)

        self.profile.refresh_from_db()
        self.assertEqual(
            (self.profile.review_note, self.profile.reviewed_by_id,
             self.profile.reviewed_at),
            before)


class ApproveThenRejectTest(ActionTestBase):
    """Consequence 2: an approved counsellor was silently un-approved."""

    def test_reject_cannot_un_approve_a_live_counsellor(self):
        self.assertEqual(self.act("approve").status_code, 200)

        r = self.act("reject", note="changed my mind",
                     as_user=self.other_admin)

        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("suspend", r.data["detail"],
                      "the refusal should name the action to use instead")
        self.assertEqual(self.status_now(), CounselorProfile.STATUS_APPROVED)
        self.assertTrue(self.profile.is_listed,
                        "a refused reject still pulled them from the directory")
        self.assertTrue(self.role_is_active(),
                        "a refused reject still revoked the COUNSELOR role")
        self.assertFalse(self.notifications("counseling.rejected").exists())

    def test_taking_a_live_counsellor_down_goes_through_suspend(self):
        """The capability is not removed, only made a deliberate two-step."""
        self.assertEqual(self.act("approve").status_code, 200)
        self.assertEqual(self.act("suspend").status_code, 200)
        self.assertEqual(self.status_now(), CounselorProfile.STATUS_SUSPENDED)

        r = self.act("reject", note="investigation closed")

        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.status_now(), CounselorProfile.STATUS_REJECTED)
        self.assertEqual(self.notifications("counseling.rejected").count(), 1)


class DoubleRejectTest(ActionTestBase):
    def test_reject_from_pending_notifies_exactly_once(self):
        r = self.act("reject", note="not enough experience")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.status_now(), CounselorProfile.STATUS_REJECTED)
        self.assertEqual(self.notifications("counseling.rejected").count(), 1)
        self.assertFalse(
            self.profile.is_listed,
            "`is_listed` defaults to True, so a rejected row claimed to be listed")

    def test_a_second_reject_is_refused_and_does_not_notify_again(self):
        self.assertEqual(self.act("reject").status_code, 200)

        r = self.act("reject", as_user=self.other_admin)

        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("already been rejected", r.data["detail"])
        self.assertEqual(self.notifications("counseling.rejected").count(), 1)


class RejectRevokesTheRoleTest(ActionTestBase):
    """Consequence 3. Reject is reachable from `suspended`, which is the state
    where the COUNSELOR role is actually live — so this is real code, not a
    defensive no-op."""

    def test_rejecting_a_suspended_counsellor_deactivates_the_role(self):
        self.act("approve")
        self.assertTrue(self.role_is_active())
        self.act("suspend")

        self.assertEqual(self.act("reject").status_code, 200)

        self.assertFalse(self.role_is_active())

    def test_the_role_row_is_deactivated_not_deleted(self):
        """So re-approving restores the same row rather than racing to
        recreate it — `_grant_counselor_role` reactivates in place."""
        self.act("approve")
        self.act("suspend")
        self.act("reject")

        self.assertTrue(
            UserRole.objects.filter(
                user=self.applicant, role__name="COUNSELOR").exists(),
            "the role row was deleted rather than deactivated")

    def test_rejecting_an_applicant_who_never_held_the_role_is_fine(self):
        self.assertFalse(self.role_is_active())
        self.assertEqual(self.act("reject").status_code, 200)
        self.assertFalse(self.role_is_active())


class LegalTransitionsTest(ActionTestBase):
    """The transitions that MUST keep working. A blanket "must be pending"
    guard would have passed every test above and broken all of these."""

    def test_approve_after_reject_is_allowed_and_restores_the_role(self):
        """CounselorApprovals.jsx offers Approve on the Rejected tab."""
        self.act("approve")
        self.act("suspend")
        self.act("reject")
        self.assertFalse(self.role_is_active())

        r = self.act("approve")

        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.status_now(), CounselorProfile.STATUS_APPROVED)
        self.assertTrue(self.role_is_active())

    def test_relist_on_an_already_approved_profile_is_allowed(self):
        """The one genuinely repeatable action. The Django admin can clear
        `is_listed` while leaving the status approved — nothing else can put
        them back, so relist must accept an approved row."""
        self.act("approve")
        CounselorProfile.objects.filter(pk=self.profile.pk).update(
            is_listed=False)

        r = self.act("relist")

        self.assertEqual(r.status_code, 200, r.data)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_listed)

    def test_the_suspend_relist_cycle_repeats(self):
        self.act("approve")
        for _ in range(2):
            self.assertEqual(self.act("suspend").status_code, 200)
            self.assertEqual(self.status_now(),
                             CounselorProfile.STATUS_SUSPENDED)
            self.assertEqual(self.act("relist").status_code, 200)
            self.assertEqual(self.status_now(),
                             CounselorProfile.STATUS_APPROVED)


class IllegalTransitionsTest(ActionTestBase):
    def test_suspend_is_refused_on_a_pending_application(self):
        r = self.act("suspend")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("reject it instead", r.data["detail"])
        self.assertEqual(self.status_now(), CounselorProfile.STATUS_PENDING)

    def test_a_second_suspend_is_refused(self):
        self.act("approve")
        self.assertEqual(self.act("suspend").status_code, 200)
        r = self.act("suspend")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("already suspended", r.data["detail"])

    def test_relist_is_refused_on_a_pending_application(self):
        r = self.act("relist")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(self.status_now(), CounselorProfile.STATUS_PENDING)

    def test_approve_is_refused_on_a_suspended_counsellor(self):
        self.act("approve")
        self.act("suspend")
        r = self.act("approve")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("relist", r.data["detail"])
        self.assertEqual(self.status_now(), CounselorProfile.STATUS_SUSPENDED)

    def test_every_refusal_reports_the_current_status(self):
        """The client re-renders from this, so a refusal that doesn't say
        where the row ended up leaves the screen showing a stale state."""
        self.act("approve")
        r = self.act("approve")
        self.assertEqual(r.data["status"], CounselorProfile.STATUS_APPROVED)


class TransitionTableInvariantTest(APITestCase):
    """Properties of LEGAL_FROM itself, so the table can't drift back into
    allowing the exact bug this file was written for."""

    def test_the_notifying_actions_cannot_run_twice_in_a_row(self):
        """The general statement of the double-approve bug: an action that
        emails must never be legal from the status it produces."""
        produces = {
            "approve": CounselorProfile.STATUS_APPROVED,
            "reject": CounselorProfile.STATUS_REJECTED,
        }
        for action, result in produces.items():
            with self.subTest(action=action):
                self.assertNotIn(
                    result, AdminApplicationActionView.LEGAL_FROM[action],
                    f"'{action}' notifies and is legal from the status it "
                    f"produces — it can fire twice on one row")

    def test_every_advertised_action_has_a_transition_rule(self):
        """ACTIONS is derived from LEGAL_FROM, so an action added to one and
        not the other would KeyError on a real request."""
        self.assertEqual(set(AdminApplicationActionView.ACTIONS),
                         set(AdminApplicationActionView.LEGAL_FROM))

    def test_every_rule_names_a_real_status(self):
        valid = set(dict(CounselorProfile.STATUS_CHOICES))
        for action, froms in AdminApplicationActionView.LEGAL_FROM.items():
            with self.subTest(action=action):
                self.assertTrue(set(froms) <= valid,
                                f"'{action}' allows a status that cannot exist")


class ActionRequestValidationTest(ActionTestBase):
    def test_an_unknown_action_is_still_a_400(self):
        r = self.act("delete")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Unknown action", r.data["detail"])
        self.assertEqual(self.status_now(), CounselorProfile.STATUS_PENDING)

    def test_a_missing_profile_404s(self):
        r = self.act("approve", profile_id=self.profile.pk + 9999)
        self.assertEqual(r.status_code, 404)

    def test_a_non_admin_cannot_act(self):
        r = self.act("approve", as_user=self.applicant)
        self.assertIn(r.status_code, (401, 403))
        self.assertEqual(self.status_now(), CounselorProfile.STATUS_PENDING)
        self.assertFalse(self.notifications("counseling.approved").exists())


class RoleSetupSanityTest(ActionTestBase):
    """`_grant_counselor_role` reactivates an existing inactive row rather
    than creating a second one — UserRole is unique on (user, role), so a
    second create would IntegrityError on the re-approve path above."""

    def test_approving_reactivates_a_pre_existing_inactive_role_row(self):
        role, _ = Role.objects.get_or_create(name="COUNSELOR")
        UserRole.objects.create(
            user=self.applicant, role=role, is_active=False)

        self.assertEqual(self.act("approve").status_code, 200)

        self.assertTrue(self.role_is_active())
        self.assertEqual(
            UserRole.objects.filter(
                user=self.applicant, role__name="COUNSELOR").count(), 1)


class RoleApprovalStampTest(ActionTestBase):
    """`_grant_counselor_role` accepted `approved_by` and dropped it, so the
    COUNSELOR role row recorded neither who approved it nor when. That is a
    visible gap, not a theoretical one: `accounts/admin.py:124` lists
    `approved_by`/`approved_at`, and `accounts/rbac_views.py:214` returns
    them per role row — a counsellor showed blank on both while every other
    admin grant path (teacher, manual RBAC, `UserRole.approve()`) stamped
    them.

    Asserted on the ROW, not on the view's response, because the view never
    returned these — a response-level test would have passed throughout."""

    def role_row(self):
        return UserRole.objects.get(
            user=self.applicant, role__name="COUNSELOR")

    def test_approving_records_which_admin_approved_and_when(self):
        self.assertEqual(self.act("approve").status_code, 200)

        ur = self.role_row()
        self.assertEqual(ur.approved_by, self.admin)
        self.assertIsNotNone(ur.approved_at)

    def test_the_reactivation_path_fills_in_a_missing_stamp(self):
        """An unstamped inactive row predates this fix (or came from
        `_revoke_counselor_role`). Re-approving must stamp it, or the fix
        only reaches accounts that never applied before."""
        role, _ = Role.objects.get_or_create(name="COUNSELOR")
        UserRole.objects.create(
            user=self.applicant, role=role, is_active=False)
        self.assertIsNone(self.role_row().approved_at)

        self.assertEqual(self.act("approve").status_code, 200)

        ur = self.role_row()
        self.assertEqual(ur.approved_by, self.admin)
        self.assertIsNotNone(ur.approved_at)

    def test_a_re_approval_keeps_the_ORIGINAL_approver_and_timestamp(self):
        """Deliberate, and mirrors the teacher branch: this stamp answers
        "when did this account first become a counsellor".
        `CounselorProfile.reviewed_by`/`reviewed_at` carry the latest
        decision, so overwriting here would leave nothing holding the
        first."""
        self.act("approve")
        first = self.role_row()
        first_at, first_by = first.approved_at, first.approved_by
        # Guard the guard: with both ends None this assertion would hold
        # vacuously, which is exactly how it read before the fix.
        self.assertIsNotNone(first_at)
        self.assertIsNotNone(first_by)
        self.act("suspend")
        self.act("reject")

        self.assertEqual(
            self.act("approve", as_user=self.other_admin).status_code, 200)

        ur = self.role_row()
        self.assertEqual(ur.approved_at, first_at)
        self.assertEqual(ur.approved_by, first_by)
        self.assertNotEqual(ur.approved_by, self.other_admin)

    def test_the_reject_path_does_not_clear_the_stamp(self):
        """`_revoke_counselor_role` deactivates with `.update(is_active=...)`,
        so the record of the original approval survives a rejection — which
        is the point of keeping the row rather than deleting it."""
        self.act("approve")
        self.act("suspend")
        self.act("reject")

        ur = self.role_row()
        self.assertFalse(ur.is_active)
        self.assertEqual(ur.approved_by, self.admin)
        self.assertIsNotNone(ur.approved_at)
