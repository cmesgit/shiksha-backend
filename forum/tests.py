"""Tests for the RBAC + forum-hardening work.

Run with: DJANGO_SETTINGS_MODULE=config.settings_test ... manage.py test forum
"""
import json

from django.test import TestCase, Client
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User, Role, UserRole, Permission, RolePermission
from forum.models import Follow, ForumCategory, ForumPost, Reply, Report
from forum.moderation_views import _remove_content


def auth_client(user):
    c = Client()
    c.cookies["access"] = str(RefreshToken.for_user(user).access_token)
    return c


class RBACModelTests(TestCase):
    def setUp(self):
        self.mod_role = Role.objects.get(name="MODERATOR")
        self.admin_role = Role.objects.get(name="ADMIN")

    def test_seed_created_permissions_and_mappings(self):
        # Scoped to the Forum/Roles categories so later seeds (e.g. the
        # documents app's Explore perms) don't perturb these baseline counts.
        self.assertEqual(
            Permission.objects.filter(category__in=["Forum", "Roles"]).count(), 13)
        self.assertEqual(
            RolePermission.objects.filter(
                role=self.mod_role, permission__category="Forum").count(), 10)
        self.assertEqual(
            RolePermission.objects.filter(
                role=self.admin_role, permission__category__in=["Forum", "Roles"]).count(), 13)

    def test_has_permission_for_moderator(self):
        u = User.objects.create(email="m@t.com", username="m")
        UserRole.objects.create(user=u, role=self.mod_role)
        u = User.objects.get(pk=u.pk)
        self.assertTrue(u.has_permission("forum.moderate"))
        self.assertFalse(u.has_permission("roles.manage"))

    def test_staff_holds_all_permissions(self):
        s = User.objects.create(email="s@t.com", username="s", is_staff=True)
        self.assertTrue(s.has_permission("roles.manage"))
        # Staff implicitly hold every seeded permission (forum + roles + docs …).
        self.assertEqual(len(s.get_permissions()), Permission.objects.count())

    def test_plain_user_holds_none(self):
        p = User.objects.create(email="p@t.com", username="p")
        self.assertFalse(p.has_permission("forum.moderate"))


class RBACEndpointTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create(
            email="admin@t.com", username="admin", is_staff=True)
        self.target = User.objects.create(email="t@t.com", username="target")
        self.c = auth_client(self.admin)

    def test_assign_and_revoke_moderator(self):
        r = self.c.post(
            f"/api/accounts/admin/users/{self.target.id}/roles/",
            data=json.dumps({"role": "MODERATOR"}),
            content_type="application/json")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(User.objects.get(pk=self.target.pk).has_permission("forum.moderate"))

        r = self.c.delete(f"/api/accounts/admin/users/{self.target.id}/roles/MODERATOR/")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(User.objects.get(pk=self.target.pk).has_permission("forum.moderate"))

    def test_admin_cannot_revoke_own_admin_role(self):
        Role.objects.get_or_create(name="ADMIN")
        UserRole.objects.get_or_create(
            user=self.admin, role=Role.objects.get(name="ADMIN"))
        r = self.c.delete(f"/api/accounts/admin/users/{self.admin.id}/roles/ADMIN/")
        self.assertEqual(r.status_code, 400)

    def test_create_custom_role_with_permissions(self):
        r = self.c.post(
            "/api/accounts/admin/roles/",
            data=json.dumps({"name": "editor", "permissions": ["forum.reports.view"]}),
            content_type="application/json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["name"], "EDITOR")
        self.assertIn("forum.reports.view", r.json()["permissions"])

    def test_non_admin_blocked(self):
        plain = User.objects.create(email="x@t.com", username="x")
        r = auth_client(plain).get("/api/accounts/admin/roles/")
        self.assertEqual(r.status_code, 403)


class ForumHardeningTests(TestCase):
    def setUp(self):
        self.author = User.objects.create(
            email="au@t.com", username="au", is_verified=True)
        self.reporter = User.objects.create(
            email="rp@t.com", username="rp", is_verified=True)
        self.post = ForumPost.objects.create(
            author=self.author, title="A question", content="body",
            kind=ForumPost.KIND_QUESTION)
        self.answer = Reply.objects.create(
            post=self.post, author=self.author, content="ans",
            kind=Reply.KIND_ANSWER)

    def test_reply_soft_delete_hides_from_list_and_counts(self):
        c = auth_client(self.author)
        self.assertEqual(c.get(f"/api/forum/threads/{self.post.id}/comments/").json()["count"], 1)
        _remove_content(Reply.objects.get(pk=self.answer.id))
        self.assertTrue(Reply.objects.get(pk=self.answer.id).is_removed)
        self.assertEqual(c.get(f"/api/forum/threads/{self.post.id}/comments/").json()["count"], 0)
        self.assertEqual(c.get(f"/api/forum/threads/{self.post.id}/").json()["answer_count"], 0)

    def test_removing_accepted_answer_clears_solved(self):
        self.post.accepted_reply = self.answer
        self.post.is_solved = True
        self.post.save()
        _remove_content(Reply.objects.get(pk=self.answer.id))
        self.post.refresh_from_db()
        self.assertIsNone(self.post.accepted_reply_id)
        self.assertFalse(self.post.is_solved)

    def test_self_report_blocked(self):
        c = auth_client(self.author)
        r = c.post("/api/forum/report/",
                   data=json.dumps({"target_type": "question", "target_id": self.post.id, "reason": "spam"}),
                   content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_duplicate_report_deduped(self):
        c = auth_client(self.reporter)
        payload = json.dumps({"target_type": "question", "target_id": self.post.id, "reason": "spam"})
        self.assertEqual(c.post("/api/forum/report/", data=payload, content_type="application/json").status_code, 201)
        self.assertEqual(c.post("/api/forum/report/", data=payload, content_type="application/json").status_code, 200)
        self.assertEqual(Report.objects.filter(resolved=False).count(), 1)

    def test_dashboard_shape(self):
        r = auth_client(self.author).get("/api/forum/dashboard/")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["stats"]["questions_asked"], 1)
        self.assertEqual(len(d["engagement"]), 7)
        self.assertIn("recent_activity", d)
        # Weekly deltas powering the dashboard trend pills.
        for k in ("questions_this_week", "answers_this_week", "saved_this_week"):
            self.assertIn(k, d["stats"])
        # The question created in setUp is within the last 7 days.
        self.assertEqual(d["stats"]["questions_this_week"], 1)

    def test_me_exposes_permissions(self):
        r = auth_client(self.author).get("/api/forum/me/")
        self.assertIn("is_moderator", r.json())
        self.assertIn("permissions", r.json())

    def test_me_resolves_followed_categories_and_questions(self):
        category = ForumCategory.objects.create(name="Physics", slug="physics")
        Follow.objects.create(
            user=self.author, target_type=Follow.TARGET_CATEGORY,
            target_key=category.slug)
        Follow.objects.create(
            user=self.author, target_type=Follow.TARGET_QUESTION,
            target_key=str(self.post.id))
        # A dangling follow (target since deleted) must be dropped, not crash.
        Follow.objects.create(
            user=self.author, target_type=Follow.TARGET_QUESTION,
            target_key="999999")
        Follow.objects.create(
            user=self.author, target_type=Follow.TARGET_CATEGORY,
            target_key="ghost-category")

        following = auth_client(self.author).get("/api/forum/me/").json()["following"]

        self.assertEqual(
            following["categories"],
            [{"id": category.id, "slug": "physics", "name": "Physics"}])
        self.assertEqual(
            following["questions"],
            [{"id": self.post.id, "title": self.post.title}])
