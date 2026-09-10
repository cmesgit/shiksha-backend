# PLACEMENT: backend/content/tests/test_reorder_homepage_sections.py
#
#     python manage.py test content.tests.test_reorder_homepage_sections
#
# Same headline assertion as test_seed_contact_copy: A DRY RUN WRITES NOTHING.
# transaction.savepoint() is a silent no-op in autocommit mode, so a dry run
# built on savepoint_rollback() commits everything it claims to be discarding —
# this repo has shipped that exact bug (seed_academy_launch). The command under
# test rolls back by raising out of transaction.atomic(); this proves it.

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from content.models import HomeSectionOrder
from content.management.commands.reorder_homepage_sections import CANONICAL_ORDER


def run(*args):
    out = StringIO()
    call_command("reorder_homepage_sections", *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def current_order():
    return list(
        HomeSectionOrder.objects.order_by("order").values_list("section", flat=True)
    )


class ReorderHomepageSectionsTests(TestCase):
    def setUp(self):
        # The migration-seeded rows already exist in the test database; start
        # from a known-scrambled state instead of assuming what they are.
        HomeSectionOrder.objects.all().delete()
        # Deliberately the OLD running order, which is what a real environment
        # seeded by content/0008 actually holds.
        self.legacy = [
            "hero", "why_shiksha", "teachers_students", "browse_categories",
            "featured_courses", "why_choose", "resources", "collaborate",
            "faq", "cta", "ticker_band",
        ]
        for index, section in enumerate(self.legacy):
            HomeSectionOrder.objects.create(section=section, order=index)

    def test_applies_canonical_order(self):
        run()
        self.assertEqual(current_order(), CANONICAL_ORDER)

    def test_dry_run_writes_nothing(self):
        before = current_order()
        output = run("--dry-run")

        self.assertEqual(current_order(), before)
        self.assertIn("rolled back", output.lower())

    def test_is_idempotent(self):
        run()
        first = current_order()

        output = run()

        self.assertEqual(current_order(), first)
        self.assertIn("nothing to do", output.lower())

    def test_leaves_is_visible_untouched(self):
        # Hiding a section is an editorial decision. Re-showing one an admin
        # deliberately hid would be worse than a wrong order.
        HomeSectionOrder.objects.filter(section="why_shiksha").update(is_visible=False)

        run()

        row = HomeSectionOrder.objects.get(section="why_shiksha")
        self.assertFalse(row.is_visible)
        # ...but it still gets placed correctly, ready for when it is shown.
        self.assertEqual(row.order, CANONICAL_ORDER.index("why_shiksha"))

    def test_unknown_section_is_appended_not_dropped(self):
        # A section added after this command was written must not vanish from
        # the homepage just because CANONICAL_ORDER has not caught up.
        HomeSectionOrder.objects.create(section="courses_hero", order=99)

        run()

        order = current_order()
        self.assertEqual(len(order), len(self.legacy) + 1)
        self.assertEqual(order[-1], "courses_hero")
        self.assertEqual(order[: len(CANONICAL_ORDER)], CANONICAL_ORDER)

    def test_missing_row_is_reported_and_skipped(self):
        HomeSectionOrder.objects.filter(section="ticker_band").delete()

        output = run()

        self.assertIn("ticker_band", output)
        self.assertEqual(current_order(), [s for s in CANONICAL_ORDER if s != "ticker_band"])

    def test_empty_table_is_a_no_op(self):
        HomeSectionOrder.objects.all().delete()

        output = run()

        self.assertEqual(current_order(), [])
        self.assertIn("no homesectionorder rows", output.lower())
