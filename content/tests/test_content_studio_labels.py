"""Content Studio Phase 7 — Labels.

The screen merges content.ContentTag and courses.CourseCategory. The TABLES
stay separate, and these tests exist mostly to keep it that way safely: the
competitive group is what lists the seven exams at all, so merging and deleting
both have to refuse when they would take that away.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from content.models import BlogPost, ContentTag, CurrentAffair, PublishStatus
from courses.models import Course, CourseCategory

User = get_user_model()

LABELS_URL = "/api/content/admin/labels/"
MERGE_URL = "/api/content/admin/labels/merge/"


class LabelTestCase(TestCase):
    def setUp(self):
        self.editor = User.objects.create_user(
            username="ed", email="ed@example.com", password="x", is_staff=True,
        )
        self.outsider = User.objects.create_user(
            username="l", email="l@example.com", password="x",
        )

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def post_with_tags(self, slug, *tags):
        p = BlogPost.objects.create(
            title=slug, slug=f"x/{slug}", status=PublishStatus.PUBLISHED,
        )
        p.tags.set(tags)
        return p


class LabelListTest(LabelTestCase):
    def test_lists_both_kinds_from_two_apps(self):
        ContentTag.objects.create(name="biology")
        CourseCategory.objects.create(name="Class 9", group="class8-12")

        body = self.client_for(self.editor).get(LABELS_URL).json()
        kinds = {r["kind"] for r in body["results"]}
        self.assertEqual(kinds, {"tag", "category"})

    def test_usage_count_counts_both_tag_relations(self):
        """A tag is used by blog posts AND current affairs. Counting only one
        would under-report and let a still-used label be deleted."""
        tag = ContentTag.objects.create(name="biology")
        self.post_with_tags("a", tag)
        affair = CurrentAffair.objects.create(
            title="A", slug="a", status=PublishStatus.PUBLISHED,
        )
        affair.tags.set([tag])

        body = self.client_for(self.editor).get(LABELS_URL).json()
        row = next(r for r in body["results"] if r["name"] == "biology")
        self.assertEqual(row["usage_count"], 2)

    def test_category_usage_counts_courses(self):
        cat = CourseCategory.objects.create(name="Class 9", group="class8-12")
        Course.objects.create(title="Science").categories.add(cat)

        body = self.client_for(self.editor).get(LABELS_URL).json()
        row = next(r for r in body["results"] if r["name"] == "Class 9")
        self.assertEqual(row["usage_count"], 1)

    def test_the_database_already_prevents_duplicate_tags(self):
        """⚠ ContentTag.slug is unique and derived from the name, so "Biology"
        and "  biology " both slugify to "biology" and the second insert fails.

        The duplicate problem the handoff describes therefore cannot exist for
        blog tags — only for CourseCategory, whose save() appends "-2" instead
        of refusing. Worth knowing before building UI to solve it."""
        from django.db import IntegrityError, transaction

        ContentTag.objects.create(name="Biology")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ContentTag.objects.create(name="  biology ")

    def test_detects_duplicate_categories(self):
        """CourseCategory does NOT refuse — it slugifies to class-9 and
        class-9-2, so two rows meaning the same thing really do coexist."""
        CourseCategory.objects.create(name="Class 9", group="class8-12")
        CourseCategory.objects.create(name="class 9", group="class8-12")

        body = self.client_for(self.editor).get(LABELS_URL).json()
        self.assertGreaterEqual(body["duplicate_count"], 1)
        dupe = next(r for r in body["results"] if r.get("duplicate_of"))
        self.assertIn("Class 9", dupe["duplicate_of"]["name"])

    def test_a_tag_and_a_category_sharing_a_name_are_not_duplicates(self):
        """They are two different things that happen to be called the same."""
        ContentTag.objects.create(name="Biology")
        CourseCategory.objects.create(name="Biology", group="boards")

        body = self.client_for(self.editor).get(LABELS_URL).json()
        self.assertEqual(body["duplicate_count"], 0)

    def test_non_staff_is_refused(self):
        self.assertEqual(
            self.client_for(self.outsider).get(LABELS_URL).status_code, 403,
        )


class LabelMergeTest(LabelTestCase):
    def test_merging_moves_every_post(self):
        keep = ContentTag.objects.create(name="biology")
        dupe = ContentTag.objects.create(name="bio")
        p1 = self.post_with_tags("a", dupe)
        p2 = self.post_with_tags("b", dupe)

        res = self.client_for(self.editor).post(
            MERGE_URL, {"kind": "tag", "from_id": dupe.id, "into_id": keep.id},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["moved"], 2)
        self.assertFalse(ContentTag.objects.filter(pk=dupe.pk).exists())
        for p in (p1, p2):
            self.assertEqual([t.name for t in p.tags.all()], ["biology"])

    def test_merging_moves_current_affairs_too(self):
        keep = ContentTag.objects.create(name="exams")
        dupe = ContentTag.objects.create(name="exam")
        affair = CurrentAffair.objects.create(
            title="A", slug="a", status=PublishStatus.PUBLISHED,
        )
        affair.tags.set([dupe])

        self.client_for(self.editor).post(
            MERGE_URL, {"kind": "tag", "from_id": dupe.id, "into_id": keep.id},
            format="json",
        )
        self.assertEqual([t.name for t in affair.tags.all()], ["exams"])

    def test_merging_a_label_into_itself_is_a_400(self):
        tag = ContentTag.objects.create(name="biology")
        res = self.client_for(self.editor).post(
            MERGE_URL, {"kind": "tag", "from_id": tag.id, "into_id": tag.id},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertTrue(ContentTag.objects.filter(pk=tag.pk).exists())

    def test_merging_categories_moves_their_courses(self):
        keep = CourseCategory.objects.create(name="Class 9", group="class8-12")
        dupe = CourseCategory.objects.create(name="class 9", group="class8-12")
        course = Course.objects.create(title="Science")
        course.categories.add(dupe)

        res = self.client_for(self.editor).post(
            MERGE_URL, {"kind": "category", "from_id": dupe.id, "into_id": keep.id},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([c.name for c in course.categories.all()], ["Class 9"])

    def test_merging_across_groups_is_refused(self):
        """⚠ The competitive group is what lists the seven exams at all.
        Merging one into a school category would take them off the site."""
        exams = CourseCategory.objects.create(name="UPSC", group="competitive")
        school = CourseCategory.objects.create(name="Class 9", group="class8-12")
        course = Course.objects.create(title="UPSC Prelims")
        course.categories.add(exams)

        res = self.client_for(self.editor).post(
            MERGE_URL, {"kind": "category", "from_id": exams.id, "into_id": school.id},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("Competitive", res.json()["detail"])
        self.assertTrue(CourseCategory.objects.filter(pk=exams.pk).exists())
        self.assertEqual([c.name for c in course.categories.all()], ["UPSC"])

    def test_a_missing_label_is_a_404_not_a_crash(self):
        tag = ContentTag.objects.create(name="biology")
        res = self.client_for(self.editor).post(
            MERGE_URL, {"kind": "tag", "from_id": tag.id, "into_id": 999999},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_non_staff_cannot_merge(self):
        a = ContentTag.objects.create(name="a")
        b = ContentTag.objects.create(name="b")
        res = self.client_for(self.outsider).post(
            MERGE_URL, {"kind": "tag", "from_id": a.id, "into_id": b.id},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(ContentTag.objects.count(), 2)


class LabelRenameDeleteTest(LabelTestCase):
    def url(self, kind, pk):
        return f"/api/content/admin/labels/{kind}/{pk}/"

    def test_renaming_updates_it_everywhere_it_is_used(self):
        """Nothing breaks: the relation is by id, so a rename is just a rename."""
        tag = ContentTag.objects.create(name="biolgy")
        post = self.post_with_tags("a", tag)

        res = self.client_for(self.editor).patch(
            self.url("tag", tag.id), {"name": "biology"}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([t.name for t in post.tags.all()], ["biology"])

    def test_renaming_to_nothing_is_refused(self):
        tag = ContentTag.objects.create(name="biology")
        res = self.client_for(self.editor).patch(
            self.url("tag", tag.id), {"name": "   "}, format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_an_unused_label_can_be_deleted(self):
        tag = ContentTag.objects.create(name="orphan")
        res = self.client_for(self.editor).delete(self.url("tag", tag.id))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(ContentTag.objects.filter(pk=tag.pk).exists())

    def test_a_used_label_is_refused_with_a_count(self):
        tag = ContentTag.objects.create(name="biology")
        self.post_with_tags("a", tag)
        res = self.client_for(self.editor).delete(self.url("tag", tag.id))
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["used_by"], 1)
        self.assertTrue(ContentTag.objects.filter(pk=tag.pk).exists())

    def test_deleting_the_last_category_in_a_group_names_the_consequence(self):
        cat = CourseCategory.objects.create(name="UPSC", group="competitive")
        Course.objects.create(title="UPSC Prelims").categories.add(cat)

        res = self.client_for(self.editor).delete(self.url("category", cat.id))
        self.assertEqual(res.status_code, 409, res.content)
        detail = res.json()["detail"]
        self.assertIn("browsing", detail)
        # "1 course rely on it" reads as a bug even though the number is right.
        self.assertIn("1 course relies on it", detail)
        self.assertTrue(CourseCategory.objects.filter(pk=cat.pk).exists())

    def test_the_plural_form_is_used_for_more_than_one(self):
        cat = CourseCategory.objects.create(name="UPSC", group="competitive")
        for t in ("A", "B"):
            Course.objects.create(title=t).categories.add(cat)
        res = self.client_for(self.editor).delete(self.url("category", cat.id))
        self.assertIn("2 courses rely on it", res.json()["detail"])
