"""Forms directory + contact enquiries.

design_handoff_admin_counsellor, Admin v2 Phase 5.

⚠ This is NOT a form builder. The artboard lists the forms the site already
has, with their fields and WHERE THE ANSWERS GO. The gap it closes is real:
`ContactMessage` has `status` and `handled_note` — built to be worked — and
had no admin API and no console screen, so enquiries landed nowhere anyone
would look.
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from content.models import ContactMessage

DIR = "/api/content/admin/forms/"
CONTACT = "/api/content/admin/forms/contact/"


def _admin():
    u = User.objects.create_user(username="fadmin", email="fadmin@e.com",
                                 password="x", is_staff=True)
    c = APIClient(); c.force_authenticate(user=u); return c


def _msg(**kw):
    return ContactMessage.objects.create(
        name=kw.get("name", "Rohit Jadhav"),
        email=kw.get("email", "rohit@e.com"),
        phone=kw.get("phone", ""),
        role=kw.get("role", ContactMessage.Role.STUDENT),
        topic=kw.get("topic", ContactMessage.Topic.ADMISSIONS),
        message=kw.get("message", "Asking about Class 12 fees"),
        consented_at=timezone.now(),
        status=kw.get("status", ContactMessage.Status.NEW),
    )


class DirectoryTest(TestCase):
    def test_requires_admin(self):
        self.assertIn(APIClient().get(DIR).status_code, (401, 403))
        u = User.objects.create_user(username="p1", email="p1@e.com", password="x")
        c = APIClient(); c.force_authenticate(user=u)
        self.assertEqual(c.get(DIR).status_code, 403)

    def test_lists_the_real_forms(self):
        keys = {f["key"] for f in _admin().get(DIR).json()["results"]}
        self.assertIn("contact", keys)
        self.assertIn("teacher_application", keys)

    def test_every_form_says_where_its_answers_go(self):
        """The whole point of the screen. A form with no destination tells an
        admin nothing about who is supposed to act on it."""
        for f in _admin().get(DIR).json()["results"]:
            self.assertTrue(f["destination"], f)

    def test_forms_owned_elsewhere_link_out_and_are_not_managed_here(self):
        """⚠ A second inbox for teacher applications would be two places to
        action the same thing — and therefore two ways to miss it."""
        forms = {f["key"]: f for f in _admin().get(DIR).json()["results"]}
        ta = forms["teacher_application"]
        self.assertFalse(ta["managed_here"])
        self.assertEqual(ta["url"], "/approvals")

    def test_contact_is_managed_here_because_nothing_else_shows_it(self):
        forms = {f["key"]: f for f in _admin().get(DIR).json()["results"]}
        self.assertTrue(forms["contact"]["managed_here"])
        self.assertIsNone(forms["contact"]["url"])

    def test_contact_counts_are_real(self):
        _msg(); _msg(status=ContactMessage.Status.CLOSED)
        forms = {f["key"]: f for f in _admin().get(DIR).json()["results"]}
        self.assertEqual(forms["contact"]["counts"]["total"], 2)
        self.assertEqual(forms["contact"]["counts"]["new"], 1)

    def test_one_broken_counter_does_not_500_the_directory(self):
        """It spans four apps; a missing migration in any of them must not
        take the screen down."""
        self.assertEqual(_admin().get(DIR).status_code, 200)


class ContactSubmissionsTest(TestCase):
    def test_requires_admin(self):
        self.assertIn(APIClient().get(CONTACT).status_code, (401, 403))

    def test_lists_newest_first(self):
        """An enquiry answered late is the failure mode, so the newest has to
        be the one you see."""
        old = _msg(name="Older")
        new = _msg(name="Newer")
        ContactMessage.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=2))
        names = [r["name"] for r in _admin().get(CONTACT).json()["results"]]
        self.assertEqual(names[0], "Newer")

    def test_filter_by_status(self):
        _msg(); _msg(status=ContactMessage.Status.CLOSED)
        c = _admin()
        self.assertEqual(len(c.get(CONTACT, {"status": "new"}).json()["results"]), 1)

    def test_search_spans_name_email_and_message(self):
        _msg(name="Sneha Patil", email="sneha@e.com", message="Batch timing question")
        c = _admin()
        for term in ("Sneha", "sneha@", "Batch timing"):
            self.assertEqual(c.get(CONTACT, {"q": term}).json()["count"], 1, term)

    def test_count_is_the_real_total_not_the_page(self):
        for i in range(3):
            _msg(name=f"P{i}")
        body = _admin().get(CONTACT).json()
        self.assertEqual(body["count"], 3)

    def test_admin_can_work_an_enquiry(self):
        m = _msg()
        res = _admin().patch(f"{CONTACT}{m.pk}/",
                             {"status": "in_progress", "handled_note": "Called back"},
                             format="json")
        self.assertEqual(res.status_code, 200, res.content)
        m.refresh_from_db()
        self.assertEqual(m.status, "in_progress")
        self.assertEqual(m.handled_note, "Called back")

    def test_what_the_visitor_SENT_cannot_be_edited(self):
        """⚠ An admin works an enquiry; they do not rewrite what was said.
        Letting the message change would destroy the only record of it."""
        m = _msg(message="Original wording")
        _admin().patch(f"{CONTACT}{m.pk}/",
                       {"message": "Rewritten", "email": "other@e.com"},
                       format="json")
        m.refresh_from_db()
        self.assertEqual(m.message, "Original wording")
        self.assertEqual(m.email, "rohit@e.com")

    def test_unknown_id_is_404(self):
        self.assertEqual(_admin().patch(f"{CONTACT}999999/", {"status": "closed"},
                                        format="json").status_code, 404)
