"""Authoring residue must not reach a reader.

The guide sources were hand-authored in Word over several years. Two kinds
of residue survived into the current import set and were being published
verbatim: CJK form-field boundary markers, and a writer's note to themselves.

These tests pin BOTH directions — the residue is dropped, and prose that
merely resembles it is not. The second direction is the one that matters:
`_NOISE_PARAGRAPH_RE` anchors on the whole paragraph precisely so that a
sentence containing "Top of Form" is left intact, and a regression to
substring matching would eat text mid-sentence with no visible error.
"""

from xml.etree import ElementTree as ET

from django.test import SimpleTestCase

from counseling.guide_import import W, _para_text


def _para(*runs: str) -> ET.Element:
    """A minimal <w:p> whose visible text is the concatenated runs."""
    p = ET.Element(W + "p")
    for run in runs:
        r = ET.SubElement(p, W + "r")
        t = ET.SubElement(r, W + "t")
        t.text = run
    return p


class NoiseParagraphTests(SimpleTestCase):
    def test_cjk_form_boundary_markers_are_dropped(self):
        # Word emits these when the document is edited in a CJK locale.
        # 6 of them sit in the current import set.
        self.assertEqual(_para_text(_para("窗体顶端")), "")
        self.assertEqual(_para_text(_para("窗体底端")), "")

    def test_english_form_boundary_markers_are_dropped(self):
        self.assertEqual(_para_text(_para("Top of Form")), "")
        self.assertEqual(_para_text(_para("Bottom of Form")), "")

    def test_writers_note_left_in_body_text_is_dropped(self):
        note = (
            "Since this is educational content, it's good to use trusted "
            "and official sources."
        )
        self.assertEqual(_para_text(_para(note)), "")

    def test_note_is_dropped_even_when_split_across_runs(self):
        # Word routinely splits a sentence across runs mid-word, so the
        # filter has to run on assembled text, not per-run.
        self.assertEqual(
            _para_text(
                _para(
                    "Since this is educational content, it's good ",
                    "to use trusted and official sources.",
                )
            ),
            "",
        )

    def test_real_prose_containing_a_marker_phrase_survives(self):
        # The whole point of anchoring: this must NOT be touched.
        prose = "Top of Form 16 you will find your employer's TAN."
        self.assertEqual(_para_text(_para(prose)), prose)

    def test_sentence_about_trusted_sources_survives(self):
        # Near-miss of the writer's note; it is genuine reader-facing advice
        # and appears in the same document.
        prose = "Take advice from trusted people, but make your own informed choice."
        self.assertEqual(_para_text(_para(prose)), prose)

    def test_ordinary_paragraph_is_unchanged(self):
        prose = "Choosing a stream after Class 10 is a decision you can revisit."
        self.assertEqual(_para_text(_para(prose)), prose)
