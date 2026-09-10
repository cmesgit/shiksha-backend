"""Navbar mega-menu curation (courses.NavMenuLink + the Studio endpoints).

The load-bearing rule is REPLACE, PER COLUMN: a column with no ACTIVE rows
must behave byte-for-byte as it does today, and a single active row must take
the column over completely. Both halves are tested here, because getting
either wrong is invisible in the admin and very visible on the public site.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from courses.models import Board, Course, CourseCategory, NavMenuLink, Stream

User = get_user_model()

NAV_URL = "/api/courses/public/nav-menu/"
ADMIN_URL = "/api/content/admin/nav-menu/"
REORDER_URL = "/api/content/admin/nav-menu/reorder/"
ADOPT_URL = "/api/content/admin/nav-menu/adopt/"


class NavCurationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cbse = Board.objects.create(
            name="CBSE", board_type=Board.TYPE_CENTRAL, is_active=True)
        cls.science = Stream.objects.create(name="science")
        cls.class9 = Course.objects.create(
            title="Class 9", board=cls.cbse, class_level=9,
            status=Course.STATUS_PUBLISHED)
        cls.comp_cat = CourseCategory.objects.create(
            name="Competitive", group=CourseCategory.GROUP_COMPETITIVE)
        cls.neet = Course.objects.create(
            title="NEET", status=Course.STATUS_PUBLISHED)
        cls.neet.categories.add(cls.comp_cat)

    def setUp(self):
        # The nav payload is cached under a version counter, and the Studio
        # permission caches its flag. Neither is rolled back between tests.
        cache.clear()
        from content.permissions import IsStudioEditor
        cache.delete(IsStudioEditor.CACHE_KEY)
        self.public = APIClient()
        self.editor = User.objects.create_user(
            username="ed", email="ed@example.com", password="x", is_staff=True)
        self.admin = APIClient()
        self.admin.force_authenticate(user=self.editor)

    def _column(self, key):
        r = self.public.get(NAV_URL)
        self.assertEqual(r.status_code, 200, r.content)
        for cat in r.data["categories"]:
            if cat["key"] == key:
                return cat
        return None

    # ── the "leave it alone" half ────────────────────────────────────

    def test_no_rows_leaves_every_column_derived(self):
        school = self._column("school")
        self.assertTrue(school["tabs"], "school must still be derived")
        comp = self._column("competitive")
        self.assertEqual(
            [l["label"] for l in comp["sections"][0]["links"]], ["NEET"])
        # Skill has no derived form at all — it stays hardcoded in the
        # frontend until somebody curates it.
        self.assertIsNone(self._column("skill"))

    def test_inactive_rows_hand_the_column_back(self):
        NavMenuLink.objects.create(
            group="competitive", label="Hidden", href="/x", is_active=False)
        comp = self._column("competitive")
        self.assertEqual(
            [l["label"] for l in comp["sections"][0]["links"]], ["NEET"],
            "an inactive row must not count as curation",
        )

    # ── the "you own it now" half ────────────────────────────────────

    def test_one_row_replaces_the_whole_derived_column(self):
        NavMenuLink.objects.create(
            group="competitive", heading="Exams", label="UPSC", href="/upsc")
        comp = self._column("competitive")
        self.assertEqual(len(comp["sections"]), 1)
        self.assertEqual(comp["sections"][0]["heading"], "Exams")
        self.assertEqual(
            [l["label"] for l in comp["sections"][0]["links"]], ["UPSC"],
            "NEET was derived; curation replaces, it does not append",
        )

    def test_school_curation_renders_as_tabs(self):
        NavMenuLink.objects.create(
            group="school", heading="Central", label="Class 9",
            course=self.class9)
        school = self._column("school")
        self.assertNotIn("sections", school)
        self.assertEqual(len(school["tabs"]), 1)
        self.assertEqual(school["tabs"][0]["label"], "Central")
        self.assertEqual(
            school["tabs"][0]["links"], [{"label": "Class 9", "to": "/courses/class-9"}])

    def test_headings_group_in_declared_order(self):
        for i, (heading, label) in enumerate([
            ("B", "second"), ("A", "first"), ("B", "third"),
        ]):
            NavMenuLink.objects.create(
                group="skill", heading=heading, label=label, href="/x",
                order=i)
        sections = self._column("skill")["sections"]
        self.assertEqual([s["heading"] for s in sections], ["B", "A"])
        self.assertEqual(
            [l["label"] for l in sections[0]["links"]], ["second", "third"])

    def test_skill_column_appears_only_once_curated(self):
        self.assertIsNone(self._column("skill"))
        NavMenuLink.objects.create(group="skill", label="Browse", href="/skill/browse")
        cache.clear()
        self.assertIsNotNone(self._column("skill"))

    def test_course_fk_survives_a_rename(self):
        link = NavMenuLink.objects.create(
            group="competitive", label="Medical entrance", course=self.neet)
        self.assertEqual(link.resolved_href, "/courses/neet")
        self.neet.title = "NEET UG 2027"
        self.neet.save()
        link.refresh_from_db()
        self.assertEqual(
            link.resolved_href, "/courses/neet",
            "the link follows the course, not its title",
        )

    def test_soon_row_renders_inert(self):
        NavMenuLink.objects.create(group="skill", label="Design", soon=True)
        links = self._column("skill")["sections"][0]["links"]
        self.assertEqual(links, [{"label": "Design", "soon": True}])

    def test_a_row_with_nowhere_to_go_is_inert_not_broken(self):
        # clean() refuses this, but a direct DB edit can still produce it.
        NavMenuLink.objects.create(group="skill", label="Orphan")
        links = self._column("skill")["sections"][0]["links"]
        self.assertEqual(links, [{"label": "Orphan", "soon": True}])

    def test_edits_are_visible_immediately(self):
        self.assertEqual(
            [l["label"] for l in self._column("competitive")["sections"][0]["links"]],
            ["NEET"])
        # No cache.clear() here on purpose: the signal must do it.
        NavMenuLink.objects.create(group="competitive", label="UPSC", href="/u")
        self.assertEqual(
            [l["label"] for l in self._column("competitive")["sections"][0]["links"]],
            ["UPSC"], "a nav edit must not wait out the 300s list TTL")

    # ── validation ───────────────────────────────────────────────────

    def test_course_and_href_together_are_refused(self):
        r = self.admin.post(ADMIN_URL, {
            "group": "skill", "label": "Both", "course": str(self.neet.id),
            "href": "/elsewhere",
        }, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("href", r.data)

    def test_a_link_needs_somewhere_to_go(self):
        r = self.admin.post(
            ADMIN_URL, {"group": "skill", "label": "Nowhere"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_unknown_group_is_refused(self):
        r = self.admin.post(
            ADMIN_URL, {"group": "footer", "label": "x", "href": "/y"},
            format="json")
        self.assertEqual(r.status_code, 400, r.content)

    # ── admin endpoints ──────────────────────────────────────────────

    def test_outsiders_cannot_read_or_write(self):
        anon = APIClient()
        self.assertIn(anon.get(ADMIN_URL).status_code, (401, 403))
        learner = User.objects.create_user(
            username="l", email="l@example.com", password="x")
        c = APIClient()
        c.force_authenticate(user=learner)
        self.assertEqual(c.get(ADMIN_URL).status_code, 403)

    def test_list_reports_derived_and_curated_state(self):
        r = self.admin.get(ADMIN_URL)
        self.assertEqual(r.status_code, 200, r.content)
        groups = {g["key"]: g for g in r.data["groups"]}
        self.assertEqual(set(groups), {"school", "competitive", "skill"})
        self.assertFalse(groups["competitive"]["curated"])
        self.assertTrue(
            groups["competitive"]["derived"],
            "the editor must be able to show what visitors see today",
        )
        self.assertTrue(any(c["title"] == "NEET" for c in r.data["courses"]))

    def test_derived_preview_survives_curating_the_same_column(self):
        """The "what your catalogue would show instead" preview must keep
        showing genuine catalogue truth after curation — that is the ONLY
        way an admin can see what curating a column dropped. It must not
        start reflecting the admin's own curated rows back at them, which
        would happen if it were read off the public endpoint's response
        instead of derived independently (the public response's `sections`
        key holds curated content once curated)."""
        self.admin.post(
            ADMIN_URL,
            {"group": "competitive", "label": "Only This One", "href": "/x"},
            format="json",
        )
        r = self.admin.get(ADMIN_URL)
        self.assertEqual(r.status_code, 200, r.content)
        competitive = {g["key"]: g for g in r.data["groups"]}["competitive"]
        self.assertTrue(competitive["curated"])
        derived_links = [
            link["label"]
            for section in competitive["derived"]
            for link in section["links"]
        ]
        self.assertIn(
            "NEET", derived_links,
            "derived preview must still show the real catalogue (NEET), "
            "not the curated row",
        )
        self.assertNotIn("Only This One", derived_links)

    def test_new_rows_append_rather_than_displace(self):
        first = self.admin.post(
            ADMIN_URL, {"group": "skill", "label": "A", "href": "/a"},
            format="json")
        second = self.admin.post(
            ADMIN_URL, {"group": "skill", "label": "B", "href": "/b"},
            format="json")
        self.assertEqual(second.status_code, 201, second.content)
        self.assertGreater(second.data["order"], first.data["order"])

    def test_patch_and_delete(self):
        link = NavMenuLink.objects.create(
            group="skill", label="Old", href="/old")
        r = self.admin.patch(
            f"{ADMIN_URL}{link.id}/", {"label": "New"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["label"], "New")
        self.assertEqual(
            self.admin.delete(f"{ADMIN_URL}{link.id}/").status_code, 204)
        self.assertFalse(NavMenuLink.objects.filter(pk=link.id).exists())

    def test_reorder_demands_the_complete_set(self):
        a = NavMenuLink.objects.create(group="skill", label="A", href="/a")
        b = NavMenuLink.objects.create(group="skill", label="B", href="/b")
        partial = self.admin.post(
            REORDER_URL, {"group": "skill", "ids": [b.id]}, format="json")
        self.assertEqual(partial.status_code, 400, partial.content)
        full = self.admin.post(
            REORDER_URL, {"group": "skill", "ids": [b.id, a.id]}, format="json")
        self.assertEqual(full.status_code, 200, full.content)
        self.assertEqual(
            [l["label"] for l in self._column("skill")["sections"][0]["links"]],
            ["B", "A"])

    # ── adopt ────────────────────────────────────────────────────────

    def test_adopt_copies_the_live_menu_without_changing_it(self):
        before = self._column("competitive")["sections"]
        r = self.admin.post(ADOPT_URL, {"group": "competitive"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        cache.clear()
        after = self._column("competitive")["sections"]
        self.assertEqual(
            [(s["heading"], [l["label"] for l in s["links"]]) for s in before],
            [(s["heading"], [l["label"] for l in s["links"]]) for s in after],
            "adopting must be a no-op for visitors",
        )

    def test_adopt_resolves_courses_back_to_the_fk(self):
        self.admin.post(ADOPT_URL, {"group": "competitive"}, format="json")
        row = NavMenuLink.objects.get(group="competitive", label="NEET")
        self.assertEqual(row.course_id, self.neet.id)
        self.assertEqual(row.href, "", "an FK row must not also carry an href")

    def test_adopt_keeps_board_filters_as_query_params(self):
        self.admin.post(ADOPT_URL, {"group": "school"}, format="json")
        board_row = NavMenuLink.objects.filter(
            group="school", label="CBSE").first()
        self.assertIsNotNone(board_row)
        self.assertIn("group=central", board_row.href)
        self.assertIn("board=cbse", board_row.href)

    def test_adopt_refuses_to_duplicate(self):
        self.assertEqual(
            self.admin.post(
                ADOPT_URL, {"group": "competitive"}, format="json").status_code,
            201)
        again = self.admin.post(
            ADOPT_URL, {"group": "competitive"}, format="json")
        self.assertEqual(again.status_code, 400, again.content)

    def test_adopt_refuses_a_column_with_no_derived_menu(self):
        r = self.admin.post(ADOPT_URL, {"group": "skill"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
