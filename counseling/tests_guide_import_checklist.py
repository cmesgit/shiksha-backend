"""Empty checkboxes in the sources become real, tickable checklists.

The guide documents carry 206 checkbox items the reader is meant to tick.
The importer used to emit them as `p` and `table` blocks, so they rendered
as the literal character `☐` — dead text. The frontend has had a working
`checklist` block (with localStorage persistence) all along; only the
importer never produced one.

The risky direction here is the TABLE detector: a false positive destroys a
real table by discarding a column. Those cases are pinned hardest.
"""

from django.test import SimpleTestCase

from counseling.guide_import import (
    _checkbox_label,
    _checklist_table_items,
    _Blocks,
)


class CheckboxLabelTests(SimpleTestCase):
    def test_both_empty_box_glyphs_are_recognised(self):
        # ☐ U+2610 (269 in the corpus) and □ U+25A1 (48) mean the same thing.
        self.assertEqual(_checkbox_label("☐ I read every day."), "I read every day.")
        self.assertEqual(_checkbox_label("□ Am I interested?"), "Am I interested?")

    def test_bare_box_has_an_empty_label(self):
        self.assertEqual(_checkbox_label("☐"), "")

    def test_tick_marks_are_not_checkboxes(self):
        # ✔ (174) and ✓ (26) are decorative bullets on advice the reader is
        # simply told. Turning them into boxes would change their meaning.
        self.assertIsNone(_checkbox_label("✔ Read the eligibility criteria."))
        self.assertIsNone(_checkbox_label("✓ Is the university recognised?"))

    def test_ordinary_prose_is_not_a_checkbox(self):
        self.assertIsNone(_checkbox_label("Choosing a stream is a big decision."))

    def test_box_must_open_the_line(self):
        self.assertIsNone(_checkbox_label("Tick the box ☐ when you are done."))


class ChecklistTableTests(SimpleTestCase):
    def test_two_column_table_with_header_becomes_items(self):
        rows = [
            ["Checklist", "Completed (✓)"],
            ["I have listened to my child's interests.", "☐"],
            ["We have explored multiple career options.", "☐"],
        ]
        self.assertEqual(
            _checklist_table_items(rows),
            [
                "I have listened to my child's interests.",
                "We have explored multiple career options.",
            ],
        )

    def test_headerless_checklist_keeps_its_first_item(self):
        # Assuming row 0 is always a header would silently drop this item.
        rows = [["Revise the syllabus.", "☐"], ["Attempt a mock paper.", "☐"]]
        self.assertEqual(
            _checklist_table_items(rows),
            ["Revise the syllabus.", "Attempt a mock paper."],
        )

    def test_box_column_may_come_first(self):
        rows = [["☐", "Collect the documents."], ["☐", "Check the deadline."]]
        self.assertEqual(
            _checklist_table_items(rows),
            ["Collect the documents.", "Check the deadline."],
        )

    # -- the dangerous direction: real tables must survive ---------------

    def test_a_real_two_column_table_is_not_a_checklist(self):
        rows = [["Stream", "Typical careers"],
                ["Science", "Engineering, medicine"],
                ["Commerce", "Accountancy, finance"]]
        self.assertIsNone(_checklist_table_items(rows))

    def test_a_table_with_one_blank_column_is_not_a_checklist(self):
        # Empty cells alone must not qualify, or any table with a blank
        # column would lose it.
        rows = [["Subject", "Notes"], ["Physics", ""], ["Chemistry", ""]]
        self.assertIsNone(_checklist_table_items(rows))

    def test_three_column_table_is_left_alone(self):
        rows = [["Task", "Owner", "☐"], ["Apply", "Me", "☐"], ["Pay", "Me", "☐"]]
        self.assertIsNone(_checklist_table_items(rows))

    def test_single_item_is_not_enough(self):
        self.assertIsNone(_checklist_table_items([["Header", "Done"], ["Only one.", "☐"]]))

    def test_rows_must_be_consistently_two_columns(self):
        rows = [["A", "☐"], ["B", "☐", "extra"]]
        self.assertIsNone(_checklist_table_items(rows))


class BlocksChecklistTests(SimpleTestCase):
    def _blocks(self):
        return _Blocks(ordered_num_ids=set())

    def test_consecutive_checkbox_paragraphs_merge_into_one_block(self):
        b = self._blocks()
        b.add_paragraph("☐ I complete my homework on time.")
        b.add_paragraph("☐ I read every day.")
        out = b.finish()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["t"], "checklist")
        self.assertEqual(
            out[0]["items"],
            ["I complete my homework on time.", "I read every day."],
        )

    def test_prose_closes_the_checklist(self):
        b = self._blocks()
        b.add_paragraph("☐ First task.")
        b.add_paragraph("Some explanation.")
        b.add_paragraph("☐ Second task.")
        kinds = [x["t"] for x in b.finish()]
        self.assertEqual(kinds, ["checklist", "p", "checklist"])

    def test_checkbox_list_items_join_the_checklist(self):
        # Word sometimes carries a checkbox line as a numbered list item.
        b = self._blocks()
        b.add_list_item((1, 0), "☐ Pack your admit card.")
        b.add_list_item((1, 0), "☐ Reach the centre early.")
        out = b.finish()
        self.assertEqual([x["t"] for x in out], ["checklist"])
        self.assertEqual(len(out[0]["items"]), 2)

    def test_checkbox_is_not_swallowed_by_an_open_callout(self):
        # A callout absorbs the prose that follows it. A checkbox line is
        # the reader's to act on, so it must break out instead.
        b = self._blocks()
        b.add_callout("📌 Remember")
        b.add_paragraph("Keep your documents ready.")
        b.add_paragraph("☐ Passport photos.")
        kinds = [x["t"] for x in b.finish()]
        self.assertEqual(kinds, ["tip", "checklist"])

    def test_bare_box_with_no_label_is_dropped(self):
        # A control with nothing to decide about is worse than nothing.
        b = self._blocks()
        b.add_paragraph("☐")
        self.assertEqual(b.finish(), [])

    def test_checklist_table_emits_a_checklist_not_a_table(self):
        b = self._blocks()
        b.add_table([["Checklist", "Done"], ["Revise.", "☐"], ["Sleep well.", "☐"]])
        out = b.finish()
        self.assertEqual([x["t"] for x in out], ["checklist"])
        self.assertEqual(out[0]["items"], ["Revise.", "Sleep well."])

    def test_ordinary_table_still_emits_a_table(self):
        rows = [["Stream", "Careers"], ["Science", "Engineering"], ["Arts", "Law"]]
        b = self._blocks()
        b.add_table(rows)
        out = b.finish()
        self.assertEqual([x["t"] for x in out], ["table"])
        self.assertEqual(out[0]["rows"], rows)
