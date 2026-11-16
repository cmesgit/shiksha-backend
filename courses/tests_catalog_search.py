"""What `?q=` on the course catalog must match.

The 13 Nov site audit reported that searching for "math", "mathematics",
"physics", "MBSE", "Mizoram Board" and "NEET" all returned "No batches match
these filters" while the matching courses were plainly visible elsewhere on
the site. The search corpus was title + description only, so:

  * subject names were never in it, though subjects are what a student
    actually searches for;
  * board names were never in it, though the search box reads "Search boards
    or classes…";
  * category names were never in it, which is how NEET is classified.

These pin the corpus. The last two also pin the thing that makes a
multi-valued join dangerous here: the row must not be duplicated, and
`subject_count` must not start counting only the subjects that matched.
"""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from courses.models import Board, Course, Subject

URL = "/api/courses/public/catalog/"


class CatalogSearchCorpusTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.mbse = Board.objects.create(name="Mizoram Board of School Education",
                                        slug="mbse", board_type="STATE")
        cls.cbse = Board.objects.create(name="CBSE", slug="cbse",
                                        board_type="NATIONAL")

        cls.class10 = Course.objects.create(
            title="Class 10", board=cls.mbse, kind="ACADEMIC",
            status=Course.STATUS_PUBLISHED, class_level=10,
            description="Full-year programme.",
        )
        # Three subjects, so subject_count has something to get wrong.
        for name in ("Mathematics", "Physics", "Social Science"):
            Subject.objects.create(course=cls.class10, name=name)

        cls.cbse10 = Course.objects.create(
            title="Class 10", board=cls.cbse, kind="ACADEMIC",
            status=Course.STATUS_PUBLISHED, class_level=10,
            description="Full-year programme.",
        )
        Subject.objects.create(course=cls.cbse10, name="Mathematics")

    def setUp(self):
        # Catalog responses are cached per querystring (list_cache_key), and a
        # stale entry from a sibling test reads exactly like a broken filter.
        cache.clear()
        self.client = APIClient()

    def _titles(self, **params):
        res = self.client.get(URL, params)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        rows = body if isinstance(body, list) else body.get("results", [])
        return rows

    def _ids(self, **params):
        return [r["id"] for r in self._titles(**params)]

    # ── subjects ────────────────────────────────────────────────────────
    def test_a_subject_name_finds_its_course(self):
        ids = self._ids(q="Mathematics")
        self.assertIn(str(self.class10.id), ids)
        self.assertIn(str(self.cbse10.id), ids)

    def test_a_partial_subject_name_finds_it(self):
        """The audit searched "math", not "Mathematics"."""
        self.assertIn(str(self.class10.id), self._ids(q="math"))

    def test_physics_finds_only_the_course_that_teaches_it(self):
        ids = self._ids(q="physics")
        self.assertIn(str(self.class10.id), ids)
        self.assertNotIn(str(self.cbse10.id), ids)

    # ── boards ──────────────────────────────────────────────────────────
    def test_a_board_slug_finds_its_courses(self):
        self.assertIn(str(self.class10.id), self._ids(q="MBSE"))

    def test_a_board_name_finds_its_courses(self):
        ids = self._ids(q="Mizoram Board")
        self.assertIn(str(self.class10.id), ids)
        self.assertNotIn(str(self.cbse10.id), ids)

    # ── the joins must not corrupt the rows ─────────────────────────────
    def test_a_matching_course_appears_exactly_once(self):
        """Joining subjects AND categories multiplies rows without distinct().
        "Class 10" matches on title; the course has three subjects."""
        ids = self._ids(q="Class 10")
        self.assertEqual(len(ids), len(set(ids)), f"duplicated rows: {ids}")

    def test_subject_count_is_not_narrowed_by_the_search(self):
        """The count-inflation trap in reverse. If the search's subjects join
        is reused by `annotate(subject_count=Count("subjects"))`, a course
        found by one subject reports a subject_count of 1 instead of 3."""
        rows = self._titles(q="Mathematics")
        row = next(r for r in rows if r["id"] == str(self.class10.id))
        self.assertEqual(
            row["subject_count"], 3,
            "searching by one subject must not change how many subjects the "
            "course reports",
        )

    def test_subject_count_is_unchanged_by_a_title_search(self):
        rows = self._titles(q="Class 10")
        row = next(r for r in rows if r["id"] == str(self.class10.id))
        self.assertEqual(row["subject_count"], 3)

    # ── nothing got looser than it should be ────────────────────────────
    def test_an_unrelated_term_still_matches_nothing(self):
        self.assertEqual(self._ids(q="zzzznotathing"), [])

    def test_search_still_composes_with_a_board_filter(self):
        ids = self._ids(q="Mathematics", board=str(self.cbse.id))
        self.assertIn(str(self.cbse10.id), ids)
        self.assertNotIn(str(self.class10.id), ids)


class CompetitiveCourseIsFindableTests(TestCase):
    """NEET is seeded as kind=COACHING with board=NULL. A board-less course
    must still be reachable by `?q=`, because the public site's fallback
    search is the only way a visitor on the School axis can discover it."""

    @classmethod
    def setUpTestData(cls):
        cls.neet = Course.objects.create(
            title="NEET Preparation", board=None, kind="COACHING",
            status=Course.STATUS_COMING_SOON,
            description="Medical entrance preparation.",
        )

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _ids(self, **params):
        res = self.client.get(URL, params)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        rows = body if isinstance(body, list) else body.get("results", [])
        return [r["id"] for r in rows]

    def test_a_board_less_course_is_found_by_title(self):
        self.assertIn(str(self.neet.id), self._ids(q="NEET"))

    def test_a_board_less_course_survives_a_board_name_search_term(self):
        """`board__name__icontains` on a NULL board must not drop the row
        from an OR — a LEFT JOIN yields NULL, which is simply not a match."""
        self.assertIn(str(self.neet.id), self._ids(q="Preparation"))
