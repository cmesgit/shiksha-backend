"""Labelled fill-in lines become real worksheet fields.

The sources write a form field as "My name: ________________". The importer
emitted those as prose, so the underscores rendered as literal underscores.
`GuideBlocks.jsx` has had a working `worksheet` block (localStorage-backed)
all along.

The corpus has 16 blank runs but only 9 are fields. The other 7 are either
mid-sentence ("I want to build my future by ____.") or bare placeholders
("₹_____"), where there is no label to put beside an input and the blank is
part of the sentence the reader reads. Those must stay prose — the tests
below pin that, because converting them would shred a sentence into a form.
"""

from django.test import SimpleTestCase

from counseling.guide_import import _Blocks, _field_key, _field_label


class FieldLabelTests(SimpleTestCase):
    def test_labelled_blank_is_a_field(self):
        self.assertEqual(_field_label("My name: ________________"), "My name")
        self.assertEqual(
            _field_label("One skill I want to improve is: ____________"),
            "One skill I want to improve is",
        )

    def test_mid_sentence_blank_is_not_a_field(self):
        # Converting this would turn a sentence into a form.
        self.assertIsNone(
            _field_label("I want to build my future by ____________________.")
        )

    def test_bare_blank_is_not_a_field(self):
        self.assertIsNone(_field_label("____"))
        self.assertIsNone(_field_label("₹_____"))

    def test_prose_without_a_blank_is_not_a_field(self):
        self.assertIsNone(_field_label("My name is written on the form."))

    def test_label_must_not_be_empty(self):
        self.assertIsNone(_field_label(": ______"))


class FieldKeyTests(SimpleTestCase):
    def test_key_is_a_stable_slug(self):
        self.assertEqual(_field_key("My name", set()), "my_name")
        self.assertEqual(_field_key("Student Name", set()), "student_name")

    def test_duplicate_labels_get_distinct_keys(self):
        taken = set()
        self.assertEqual(_field_key("Date", taken), "date")
        self.assertEqual(_field_key("Date", taken), "date_2")
        self.assertEqual(_field_key("Date", taken), "date_3")

    def test_unslugglable_label_still_yields_a_key(self):
        self.assertEqual(_field_key("—", set()), "field")


class BlocksWorksheetTests(SimpleTestCase):
    def _blocks(self):
        return _Blocks(ordered_num_ids=set())

    def test_consecutive_fields_merge_into_one_worksheet(self):
        b = self._blocks()
        b.add_paragraph("Student Name: ____________")
        b.add_paragraph("Date: ____________")
        b.add_paragraph("Signature: ____________")
        out = b.finish()
        self.assertEqual([x["t"] for x in out], ["worksheet"])
        self.assertEqual(
            [f["label"] for f in out[0]["fields"]],
            ["Student Name", "Date", "Signature"],
        )

    def test_fields_authored_as_list_items_join_the_worksheet(self):
        # 5 of the 9 real fields are bulleted; missing this splits one
        # worksheet across two block types.
        b = self._blocks()
        b.add_list_item((1, 0), "My three greatest strengths are: ________")
        b.add_list_item((1, 0), "My favourite activities are: ________")
        out = b.finish()
        self.assertEqual([x["t"] for x in out], ["worksheet"])
        self.assertEqual(len(out[0]["fields"]), 2)

    def test_prose_closes_the_worksheet(self):
        b = self._blocks()
        b.add_paragraph("Name: ______")
        b.add_paragraph("Now share this with your teacher.")
        b.add_paragraph("Date: ______")
        self.assertEqual([x["t"] for x in b.finish()], ["worksheet", "p", "worksheet"])

    def test_mid_sentence_blank_stays_prose(self):
        b = self._blocks()
        b.add_paragraph("I want to build my future by ____________.")
        self.assertEqual([x["t"] for x in b.finish()], ["p"])

    def test_field_is_not_swallowed_by_an_open_callout(self):
        b = self._blocks()
        b.add_callout("📌 Remember")
        b.add_paragraph("Fill this in honestly.")
        b.add_paragraph("My name: ______")
        self.assertEqual([x["t"] for x in b.finish()], ["tip", "worksheet"])

    def test_checklist_and_worksheet_do_not_merge(self):
        b = self._blocks()
        b.add_paragraph("☐ Revise the syllabus.")
        b.add_paragraph("My name: ______")
        b.add_paragraph("☐ Sleep well.")
        self.assertEqual(
            [x["t"] for x in b.finish()], ["checklist", "worksheet", "checklist"]
        )

    def test_keys_are_unique_within_a_worksheet(self):
        b = self._blocks()
        b.add_paragraph("Date: ______")
        b.add_paragraph("Date: ______")
        keys = [f["key"] for f in b.finish()[0]["fields"]]
        self.assertEqual(len(keys), len(set(keys)))
