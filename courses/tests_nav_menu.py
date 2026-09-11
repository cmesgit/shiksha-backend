"""Cover for the navbar mega-menu payload (`/courses/public/nav-menu/`).

The bug these tests exist for: the mobile drawer flattens every board tab into
one list (`Navbar.jsx` does `cat.tabs.flatMap(t => t.links)`). Prod carries two
boards, CBSE and MBSE, each offering the same nine classes — so the flattened
list held nine duplicated labels ("Class 9" twice, with nothing to tell the
boards apart) and colliding React keys behind them.

Qualifying the label only when a single TAB held several boards could never fix
that: the ambiguity is created ACROSS tabs, downstream of this payload.
"""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from courses.models import Board, Course, Stream

NAV_URL = "/api/courses/public/nav-menu/"


class NavMenuLabelTests(TestCase):
    """Two boards offering the same classes must stay tellable apart."""

    @classmethod
    def setUpTestData(cls):
        cls.cbse = Board.objects.create(
            name="CBSE", board_type=Board.TYPE_CENTRAL, is_active=True)
        cls.mbse = Board.objects.create(
            name="MBSE", board_type=Board.TYPE_STATE, is_active=True)
        cls.science = Stream.objects.create(name="science")

        # The prod shape: both boards offer Class 9, and Class 11 Science.
        for board in (cls.cbse, cls.mbse):
            Course.objects.create(
                title=f"{board.name} Class 9", board=board, class_level=9,
                status=Course.STATUS_PUBLISHED)
            Course.objects.create(
                title=f"{board.name} Class 11 Sci", board=board, class_level=11,
                stream=cls.science, status=Course.STATUS_PUBLISHED)

    def setUp(self):
        # The view caches its payload; a stale entry would let these tests pass
        # against the previous test's data.
        cache.clear()
        self.client = APIClient()

    def _school_tabs(self):
        r = self.client.get(NAV_URL)
        self.assertEqual(r.status_code, 200, r.content)
        school = next(c for c in r.data["categories"] if c["key"] == "school")
        return school["tabs"]

    def _flattened(self):
        """Exactly what the mobile drawer renders."""
        return [l["label"] for t in self._school_tabs() for l in t["links"]]

    def test_class_labels_carry_their_board(self):
        labels = self._flattened()
        self.assertIn("Class 9 · CBSE", labels)
        self.assertIn("Class 9 · MBSE", labels)
        self.assertNotIn("Class 9", labels)

    def test_the_stream_survives_alongside_the_board(self):
        labels = self._flattened()
        self.assertIn("Class 11 · Science · CBSE", labels)
        self.assertIn("Class 11 · Science · MBSE", labels)

    def test_the_flattened_drawer_has_no_duplicate_labels(self):
        """The actual regression. Guards the drawer, not the desktop panel."""
        labels = self._flattened()
        dupes = {l for l in labels if labels.count(l) > 1}
        self.assertEqual(dupes, set(), f"duplicated in the drawer: {dupes}")

    def test_the_board_itself_is_still_offered(self):
        """Qualifying the classes must not cost the whole-board entry."""
        labels = self._flattened()
        self.assertIn("CBSE", labels)
        self.assertIn("MBSE", labels)

    def test_a_single_board_tab_is_qualified_too(self):
        """Each tab here holds ONE board, which is exactly the case the old
        `multi` check skipped — and exactly where the drawer bug lived."""
        tabs = self._school_tabs()
        central = next(t for t in tabs if t["id"] == "central")
        self.assertEqual(
            sum(1 for b in Board.objects.filter(board_type=Board.TYPE_CENTRAL)
                if b.is_active), 1)
        self.assertIn("Class 9 · CBSE", [l["label"] for l in central["links"]])

    def test_a_coming_soon_class_is_labelled_and_not_linked(self):
        Course.objects.create(
            title="CBSE Class 12", board=self.cbse, class_level=12,
            status=Course.STATUS_COMING_SOON)
        cache.clear()
        central = next(t for t in self._school_tabs() if t["id"] == "central")
        soon = next(l for l in central["links"] if l["label"] == "Class 12 · CBSE")
        self.assertTrue(soon.get("soon"))
        self.assertNotIn("to", soon)

    def test_a_draft_class_never_reaches_the_navbar(self):
        Course.objects.create(
            title="CBSE Class 7", board=self.cbse, class_level=7,
            status=Course.STATUS_DRAFT)
        cache.clear()
        self.assertNotIn("Class 7 · CBSE", self._flattened())


class NavMenuBoardTypeLabelTests(TestCase):
    """The navbar's board-group wording, and the wire value under it.

    `_derive_courses_menu` builds three visitor-facing strings out of
    `Board.TYPE_CHOICES`'s display label — the tab, its heading, and the
    "View all" link. Nothing covered them, so relabelling the choice changed
    the public navbar with no test saying a word.

    The second half matters more than the first: the tab `id` and the
    `selectedBoardGroup` it carries are the LOWERCASED STORED VALUE, not the
    label. They travel in shared `?group=` links and in homepage CMS
    `link_state` rows already saved in the database, so they must survive any
    future rewording. A test that only checked the labels would let someone
    "finish the rename" by changing the value and break all of those.
    """

    @classmethod
    def setUpTestData(cls):
        cls.cbse = Board.objects.create(
            name="CBSE", board_type=Board.TYPE_CENTRAL, is_active=True)
        cls.mbse = Board.objects.create(
            name="MBSE", board_type=Board.TYPE_STATE, is_active=True)
        Course.objects.create(
            title="CBSE Class 9", board=cls.cbse, class_level=9,
            status=Course.STATUS_PUBLISHED)
        Course.objects.create(
            title="MBSE Class 9", board=cls.mbse, class_level=9,
            status=Course.STATUS_PUBLISHED)

    def setUp(self):
        cache.clear()

    def _tab(self, tab_id):
        r = self.client.get(NAV_URL)
        self.assertEqual(r.status_code, 200, r.content)
        school = next(c for c in r.data["categories"] if c["key"] == "school")
        return next(t for t in school["tabs"] if t["id"] == tab_id)

    def test_the_national_tab_reads_national_not_central(self):
        tab = self._tab("central")
        self.assertEqual(tab["label"], "National Boards")
        self.assertEqual(tab["heading"], "National Board Courses")
        self.assertEqual(tab["viewAll"]["label"], "View All National Boards")

    def test_the_state_tab_is_untouched(self):
        tab = self._tab("state")
        self.assertEqual(tab["label"], "State Boards")
        self.assertEqual(tab["heading"], "State Board Courses")

    def test_the_word_central_is_gone_from_the_whole_payload(self):
        """Belt and braces: the label is interpolated in three places and a
        fourth could be added without updating the assertions above."""
        body = self.client.get(NAV_URL).content.decode()
        self.assertNotIn("Central", body)

    def test_the_group_wire_value_is_still_central(self):
        """The label moved; the value must not. This is the deep-link
        contract shared links and saved CMS rows depend on."""
        tab = self._tab("central")
        self.assertEqual(tab["id"], "central")
        self.assertEqual(
            tab["viewAll"]["state"]["selectedBoardGroup"], "central")
        board_link = next(l for l in tab["links"] if l["label"] == "CBSE")
        self.assertEqual(board_link["state"]["selectedBoardGroup"], "central")

    def test_the_stored_choice_value_is_still_central(self):
        """Guards the model itself, not just the payload."""
        self.assertEqual(Board.TYPE_CENTRAL, "CENTRAL")
        self.assertEqual(
            self.cbse.get_board_type_display(), "National")
