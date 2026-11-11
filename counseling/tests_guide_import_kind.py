from django.test import SimpleTestCase

from counseling.guide_import import chapter_kind


class ChapterKindTests(SimpleTestCase):
    def test_numbered_activity_and_action_plan_headings(self):
        self.assertEqual(chapter_kind("11. Activities & Worksheets"), "worksheet")
        self.assertEqual(chapter_kind("12: Student Action Plan"), "action_plan")
        self.assertEqual(chapter_kind("13. References"), "references")
        self.assertEqual(chapter_kind("3. Skills for Life"), "content")
