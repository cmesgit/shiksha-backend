"""Phase 2a — the homepage Featured grid's filter tabs become CMS data.

The tab list used to be hardcoded in three repos at once
(`ShowcaseCourse.CATEGORY_CHOICES`, shiksha-frontend's `COURSE_TABS`, and a
literal array in Admin-dashboard's `CardFormModal.jsx`), kept in sync by
comment. These tests pin the behaviour that replaces it, and in particular the
two ways this change could have broken live data:

  * every card already on prod carries one of the three original slugs, so the
    seed migration has to make them valid — otherwise the first save of any
    existing card fails validation;
  * switching a tab OFF must not make cards still tagged with it unsaveable.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from content.models import PublishStatus, ShowcaseCategory, ShowcaseCourse

User = get_user_model()

FEATURED_URL = "/api/courses/public/featured/"
CATEGORIES_URL = "/api/content/admin/showcase-categories/"
REORDER_URL = "/api/content/admin/showcase/reorder/"


def make_card(title, categories=None, order=0, status=PublishStatus.PUBLISHED):
    return ShowcaseCourse.objects.create(
        title=title, level_label="L", fact_line="f",
        categories=categories if categories is not None else [],
        order=order, status=status,
    )


class SeedMigrationTests(TestCase):
    """The seed is load-bearing: clean() now validates against this table."""

    def test_three_original_tabs_exist(self):
        slugs = list(
            ShowcaseCategory.objects.order_by("order").values_list("slug", flat=True)
        )
        self.assertEqual(slugs, ["boards", "class8-12", "competitive"])

    def test_labels_match_what_the_homepage_renders(self):
        # "Class 8–12" uses an EN DASH (U+2013). The tab label is public copy;
        # a hyphen here would silently change the live site.
        labels = dict(ShowcaseCategory.objects.values_list("slug", "label"))
        self.assertEqual(labels["class8-12"], "Class 8–12")
        self.assertEqual(labels["boards"], "Boards")
        self.assertEqual(labels["competitive"], "Competitive")

    def test_a_card_carrying_a_seeded_slug_still_validates(self):
        # The regression this seed exists to prevent.
        card = make_card("Class 9", ["class8-12"])
        card.full_clean()  # must not raise


class CategoryModelTests(TestCase):
    def test_slug_all_is_reserved(self):
        with self.assertRaises(ValidationError) as ctx:
            ShowcaseCategory(slug="all", label="All").full_clean()
        self.assertIn("slug", ctx.exception.message_dict)

    def test_slug_all_is_reserved_case_insensitively(self):
        with self.assertRaises(ValidationError):
            ShowcaseCategory(slug="ALL", label="All").full_clean()


class CardCategoryValidationTests(TestCase):
    def test_unknown_slug_is_rejected(self):
        card = make_card("X", [])
        card.categories = ["not-a-tab"]
        with self.assertRaises(ValidationError) as ctx:
            card.full_clean()
        self.assertIn("categories", ctx.exception.message_dict)

    def test_a_newly_added_tab_is_valid_with_no_deploy(self):
        # The whole point of the change.
        ShowcaseCategory.objects.create(slug="skills", label="Skills", order=9)
        card = make_card("Y", [])
        card.categories = ["skills"]
        card.full_clean()  # must not raise

    def test_an_inactive_tab_does_not_break_cards_still_tagged_with_it(self):
        """Hiding a tab must not turn into corrupting content.

        clean() validates against ALL rows, not just active ones — otherwise
        switching a tab off would make every card carrying it unsaveable.
        """
        ShowcaseCategory.objects.filter(slug="competitive").update(is_active=False)
        card = make_card("NEET", ["competitive"])
        card.full_clean()  # must not raise

    def test_empty_categories_is_allowed(self):
        make_card("Untagged", []).full_clean()


class FeaturedPayloadTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # the payload is cached per courses-version

    def test_tabs_are_served_with_the_cards(self):
        make_card("A", ["boards"])
        data = self.client.get(FEATURED_URL).json()
        self.assertIn("tabs", data)
        self.assertEqual(
            data["tabs"],
            [
                {"id": "boards", "label": "Boards"},
                {"id": "class8-12", "label": "Class 8–12"},
                {"id": "competitive", "label": "Competitive"},
            ],
        )

    def test_inactive_tabs_are_not_served(self):
        ShowcaseCategory.objects.filter(slug="competitive").update(is_active=False)
        tabs = self.client.get(FEATURED_URL).json()["tabs"]
        self.assertNotIn("competitive", [t["id"] for t in tabs])

    def test_all_is_never_a_served_tab(self):
        # The homepage renders its own "All"; a second one would filter for
        # cards literally tagged "all" and always be empty.
        tabs = self.client.get(FEATURED_URL).json()["tabs"]
        self.assertNotIn("all", [t["id"] for t in tabs])

    def test_tabs_respect_order(self):
        # `order` is a PositiveSmallIntegerField, so a new tab cannot sort ahead
        # of the seeded ones by going negative — it is renumbered instead. This
        # asserts the ordering is applied at all, using a value the column
        # actually accepts.
        ShowcaseCategory.objects.create(slug="skills", label="Skills", order=9)
        tabs = self.client.get(FEATURED_URL).json()["tabs"]
        self.assertEqual(tabs[-1]["id"], "skills")

        ShowcaseCategory.objects.filter(slug="skills").update(order=0)
        ShowcaseCategory.objects.get(slug="skills").save()  # bump the cache
        tabs = self.client.get(FEATURED_URL).json()["tabs"]
        # order 0 ties with "boards"; the Meta tiebreak is slug, and
        # "boards" < "skills".
        self.assertEqual([t["id"] for t in tabs[:2]], ["boards", "skills"])

    def test_renaming_a_tab_is_visible_immediately(self):
        """Guards the cross-app cache bump in courses/cache.py.

        The featured payload is cached under the COURSES version, so without
        ShowcaseCategory in that app's tracked models a rename would not show
        for up to LIST_TTL (300s).
        """
        self.client.get(FEATURED_URL)  # prime the cache
        ShowcaseCategory.objects.filter(slug="boards").update(label="Exam Boards")
        # update() does not fire post_save, so bump explicitly the way a real
        # save would; the assertion is that the KEY changes, not that .update()
        # is magic.
        ShowcaseCategory.objects.get(slug="boards").save()
        tabs = self.client.get(FEATURED_URL).json()["tabs"]
        self.assertEqual(tabs[0]["label"], "Exam Boards")


class CategoryAdminApiTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        from content.permissions import IsStudioEditor
        cache.delete(IsStudioEditor.CACHE_KEY)
        cache.clear()
        self.editor = User.objects.create_user(
            username="ed", email="ed@example.com", password="x", is_staff=True,
        )
        self.client_ = APIClient()
        self.client_.force_authenticate(user=self.editor)

    def test_editor_can_create_a_tab(self):
        r = self.client_.post(
            CATEGORIES_URL, {"slug": "skills", "label": "Skills", "order": 5},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(ShowcaseCategory.objects.filter(slug="skills").exists())

    def test_creating_the_reserved_slug_is_refused(self):
        r = self.client_.post(
            CATEGORIES_URL, {"slug": "all", "label": "All"}, format="json",
        )
        self.assertEqual(r.status_code, 400)

    def test_card_count_is_reported(self):
        make_card("A", ["boards"])
        make_card("B", ["boards", "competitive"])
        rows = {c["slug"]: c["card_count"] for c in self.client_.get(CATEGORIES_URL).data}
        self.assertEqual(rows["boards"], 2)
        self.assertEqual(rows["competitive"], 1)
        self.assertEqual(rows["class8-12"], 0)

    def test_delete_is_refused_while_cards_use_the_tab(self):
        """There is no FK, so a delete cannot cascade — it would orphan the
        slug in every tagged card and break their next save."""
        make_card("A", ["boards"])
        cat = ShowcaseCategory.objects.get(slug="boards")
        r = self.client_.delete(f"{CATEGORIES_URL}{cat.pk}/")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(ShowcaseCategory.objects.filter(pk=cat.pk).exists())

    def test_unused_tab_can_be_deleted(self):
        cat = ShowcaseCategory.objects.create(slug="skills", label="Skills")
        r = self.client_.delete(f"{CATEGORIES_URL}{cat.pk}/")
        self.assertEqual(r.status_code, 204)

    def test_outsider_is_refused(self):
        outsider = User.objects.create_user(
            username="l", email="l@example.com", password="x",
        )
        c = APIClient()
        c.force_authenticate(user=outsider)
        self.assertIn(c.get(CATEGORIES_URL).status_code, (401, 403))


class CardReorderTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        from content.permissions import IsStudioEditor
        cache.delete(IsStudioEditor.CACHE_KEY)
        self.editor = User.objects.create_user(
            username="ed", email="ed@example.com", password="x", is_staff=True,
        )
        self.client_ = APIClient()
        self.client_.force_authenticate(user=self.editor)
        self.a = make_card("A", order=0)
        self.b = make_card("B", order=1)
        self.c = make_card("C", order=2)

    def test_full_set_reorders(self):
        r = self.client_.post(
            REORDER_URL,
            {"cards": [str(self.c.pk), str(self.a.pk), str(self.b.pk)]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(
            list(ShowcaseCourse.objects.order_by("order").values_list("title", flat=True)),
            ["C", "A", "B"],
        )

    def test_partial_list_is_refused(self):
        """Same rule as HomeSectionOrder.reorder: a stale tab sending a subset
        would silently reshuffle the homepage."""
        r = self.client_.post(
            REORDER_URL, {"cards": [str(self.a.pk)]}, format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("missing", r.data)

    def test_duplicate_ids_are_refused(self):
        r = self.client_.post(
            REORDER_URL,
            {"cards": [str(self.a.pk), str(self.a.pk), str(self.b.pk)]},
            format="json",
        )
        self.assertEqual(r.status_code, 400)

    def test_unknown_id_is_refused(self):
        import uuid
        r = self.client_.post(
            REORDER_URL,
            {"cards": [str(self.a.pk), str(self.b.pk), str(self.c.pk), str(uuid.uuid4())]},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("unexpected", r.data)

    def test_empty_list_is_refused(self):
        r = self.client_.post(REORDER_URL, {"cards": []}, format="json")
        self.assertEqual(r.status_code, 400)
