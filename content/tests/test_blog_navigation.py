"""Blog listing sort + the related/prev-next endpoint.

The 13 Nov audit found an article page with no way onward: no related posts,
no next/previous chapter, and a listing with subject and class filters but no
sort control at all. Both were reported as missing features; both were really
unused capability — `chapter_number` and the `(class_level, subject)` index
have been on BlogPost since launch, and `view_count` has been incremented on
every detail view the whole time.
"""
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta

from content.models import BlogPost, ClassLevel, Locale, PublishStatus, Subject

LIST = "/api/content/blogs/"


def _post(slug, title, *, chapter=None, cls=ClassLevel.CLASS_9,
          subject=Subject.SCIENCE, days_ago=0, views=0,
          status=PublishStatus.PUBLISHED):
    p = BlogPost.objects.create(
        slug=slug, title=title, body_html="<p>x</p>",
        class_level=cls, subject=subject, chapter_number=chapter,
        status=status, view_count=views,
        publish_at=timezone.now() - timedelta(days=days_ago),
    )
    return p


class BlogListSortTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        _post("a", "Alpha", days_ago=30, views=5)
        _post("b", "Bravo", days_ago=10, views=99)
        _post("c", "Charlie", days_ago=1, views=0)

    def setUp(self):
        cache.clear()

    def _titles(self, **params):
        res = self.client.get(LIST, params)
        self.assertEqual(res.status_code, 200, res.content)
        return [r["title"] for r in res.json()["results"]]

    def test_default_is_newest_first(self):
        """Unchanged from Meta.ordering — every existing caller depends on it."""
        self.assertEqual(self._titles(), ["Charlie", "Bravo", "Alpha"])

    def test_sort_newest_matches_the_default(self):
        self.assertEqual(self._titles(sort="newest"), self._titles())

    def test_sort_oldest(self):
        self.assertEqual(self._titles(sort="oldest"), ["Alpha", "Bravo", "Charlie"])

    def test_sort_popular_uses_view_count(self):
        self.assertEqual(self._titles(sort="popular")[0], "Bravo")

    def test_sort_title_is_alphabetical(self):
        self.assertEqual(self._titles(sort="title"), ["Alpha", "Bravo", "Charlie"])

    def test_an_unknown_sort_falls_back_rather_than_400ing(self):
        """A stale bookmark must not break the listing."""
        self.assertEqual(self._titles(sort="nonsense"), self._titles())

    def test_sort_is_not_an_open_ordering_param(self):
        """`?sort=` is whitelisted. An open ordering on a public endpoint
        would let a caller sort by columns the serializer never exposes."""
        self.assertEqual(self._titles(sort="-id"), self._titles())

    def test_sort_composes_with_a_filter(self):
        _post("d", "Delta", cls=ClassLevel.CLASS_10, days_ago=0)
        self.assertEqual(self._titles(sort="oldest", class_level="9"),
                         ["Alpha", "Bravo", "Charlie"])


class BlogRelatedTests(TestCase):
    """GET /api/content/blogs/<slug>/related/"""

    @classmethod
    def setUpTestData(cls):
        # A Class 9 Science series, deliberately created out of order so the
        # endpoint cannot pass by accident on insertion order.
        cls.ch2 = _post("class-9/science/ch-2", "Chapter 2", chapter=2)
        cls.ch4 = _post("class-9/science/ch-4", "Chapter 4", chapter=4)
        cls.ch1 = _post("class-9/science/ch-1", "Chapter 1", chapter=1)
        cls.ch3 = _post("class-9/science/ch-3", "Chapter 3", chapter=3)
        # Different subject and different class — neither is a sibling.
        cls.maths = _post("class-9/maths/ch-1", "Maths 1", chapter=1,
                          subject=Subject.MATHEMATICS)
        cls.c10 = _post("class-10/science/ch-1", "C10 Science 1", chapter=1,
                        cls=ClassLevel.CLASS_10)

    def _get(self, post):
        res = self.client.get(f"/api/content/blogs/{post.slug}/related/")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def test_prev_and_next_are_the_adjacent_chapters(self):
        body = self._get(self.ch3)
        self.assertEqual(body["prev"]["title"], "Chapter 2")
        self.assertEqual(body["next"]["title"], "Chapter 4")

    def test_the_first_chapter_has_no_prev(self):
        body = self._get(self.ch1)
        self.assertIsNone(body["prev"])
        self.assertEqual(body["next"]["title"], "Chapter 2")

    def test_the_last_chapter_has_no_next(self):
        body = self._get(self.ch4)
        self.assertEqual(body["prev"]["title"], "Chapter 3")
        self.assertIsNone(body["next"])

    def test_related_excludes_self_prev_and_next(self):
        body = self._get(self.ch3)
        titles = [r["title"] for r in body["related"]]
        self.assertNotIn("Chapter 3", titles)
        self.assertNotIn("Chapter 2", titles, "prev must not repeat in related")
        self.assertNotIn("Chapter 4", titles, "next must not repeat in related")
        self.assertEqual(titles, ["Chapter 1"])

    def test_related_does_not_cross_subject_or_class(self):
        titles = [r["title"] for r in self._get(self.ch1)["related"]]
        self.assertNotIn("Maths 1", titles)
        self.assertNotIn("C10 Science 1", titles)

    def test_a_post_with_no_chapter_number_still_gets_related(self):
        loose = _post("class-9/science/essay", "Loose Essay", chapter=None)
        body = self._get(loose)
        self.assertIsNone(body["prev"])
        self.assertIsNone(body["next"])
        self.assertTrue(body["related"], "siblings exist; the footer must not be empty")

    def test_an_only_child_falls_back_to_the_same_subject(self):
        """A post alone in its class+subject must not render an empty footer
        when the same subject exists at another level."""
        solo = _post("class-12/science/only", "Only One", cls=ClassLevel.CLASS_12)
        titles = [r["title"] for r in self._get(solo)["related"]]
        self.assertTrue(titles)
        self.assertNotIn("Only One", titles)

    def test_related_is_capped(self):
        for n in range(5, 15):
            _post(f"class-9/science/ch-{n}", f"Chapter {n}", chapter=n)
        self.assertLessEqual(len(self._get(self.ch1)["related"]), 4)

    def test_unpublished_siblings_are_not_returned(self):
        _post("class-9/science/ch-99", "Draft Chapter", chapter=99,
              status=PublishStatus.DRAFT)
        titles = [r["title"] for r in self._get(self.ch4)["related"]]
        self.assertNotIn("Draft Chapter", titles)
        self.assertIsNone(self._get(self.ch4)["next"],
                          "a draft must not become the next chapter")

    def test_the_route_is_not_swallowed_by_the_path_slug_detail_route(self):
        """`blogs/<path:slug>/` matches slashes, so if the related route were
        declared after it, this would 404 as a post named '.../ch-1/related'."""
        res = self.client.get("/api/content/blogs/class-9/science/ch-1/related/")
        self.assertEqual(res.status_code, 200, res.content)

    def test_an_unknown_slug_404s(self):
        res = self.client.get("/api/content/blogs/nope/not-a-post/related/")
        self.assertEqual(res.status_code, 404)

    def test_related_does_not_increment_view_count(self):
        """Only the detail view counts a read. A footer fetch must not."""
        before = BlogPost.objects.get(pk=self.ch1.pk).view_count
        self._get(self.ch1)
        self.assertEqual(BlogPost.objects.get(pk=self.ch1.pk).view_count, before)
