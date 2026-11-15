"""POST /api/counseling/counselor/reapply/ — rejected → pending.

design_handoff_admin_counsellor, counsellor Phase 2 ("Rejected → Update and
re-apply").

⚠ WHY THIS NEEDED A DEDICATED ENDPOINT. `status` is read-only on
CounselorSelfSerializer, deliberately — otherwise anyone could approve
themselves with a PATCH. The side effect was that a rejected applicant could
edit their details forever and never re-enter the admin queue, while the
console's rejected screen promised "our team can take another look".
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from counseling.models import CounselorProfile

URL = "/api/counseling/counselor/reapply/"


class ReapplyTest(TestCase):
    def _user(self, name, status=None):
        u = User.objects.create_user(
            username=name, email=f"{name}@e.com", password="x")
        if status:
            CounselorProfile.objects.create(
                user=u, display_name=name.title(), status=status,
                review_note="Needs a scanned certificate.")
        return u

    def _client(self, u):
        c = APIClient(); c.force_authenticate(user=u); return c

    def test_rejected_goes_back_to_pending(self):
        u = self._user("rj1", CounselorProfile.STATUS_REJECTED)
        res = self._client(u).post(URL)
        self.assertEqual(res.status_code, 200, res.content)
        u.counselor_profile.refresh_from_db()
        self.assertEqual(u.counselor_profile.status, CounselorProfile.STATUS_PENDING)

    def test_the_superseded_rejection_note_is_cleared(self):
        """Leaving it would show the applicant last round's reason while they
        wait for this round's verdict."""
        u = self._user("rj2", CounselorProfile.STATUS_REJECTED)
        self._client(u).post(URL)
        u.counselor_profile.refresh_from_db()
        self.assertEqual(u.counselor_profile.review_note, "")
        self.assertIsNone(u.counselor_profile.reviewed_by)
        self.assertIsNone(u.counselor_profile.reviewed_at)

    def test_suspended_cannot_un_suspend_itself(self):
        """⚠ THE SECURITY POINT. Suspension is an admin action; letting a
        suspended counsellor re-apply their way out of it would make the
        action meaningless."""
        u = self._user("rj3", CounselorProfile.STATUS_SUSPENDED)
        res = self._client(u).post(URL)
        self.assertEqual(res.status_code, 400, res.content)
        u.counselor_profile.refresh_from_db()
        self.assertEqual(u.counselor_profile.status, CounselorProfile.STATUS_SUSPENDED)

    def test_approved_cannot_demote_itself_to_pending(self):
        u = self._user("rj4", CounselorProfile.STATUS_APPROVED)
        res = self._client(u).post(URL)
        self.assertEqual(res.status_code, 400, res.content)
        u.counselor_profile.refresh_from_db()
        self.assertEqual(u.counselor_profile.status, CounselorProfile.STATUS_APPROVED)

    def test_pending_is_refused_rather_than_silently_accepted(self):
        """It is already in the queue; a 200 would imply something happened."""
        u = self._user("rj5", CounselorProfile.STATUS_PENDING)
        self.assertEqual(self._client(u).post(URL).status_code, 400)

    def test_no_profile_is_404(self):
        u = self._user("rj6")
        self.assertEqual(self._client(u).post(URL).status_code, 404)

    def test_anonymous_is_refused(self):
        self.assertIn(APIClient().post(URL).status_code, (401, 403))
