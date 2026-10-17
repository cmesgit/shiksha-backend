from django.test import TestCase

from .models import Course


class PublicCourseVideoTests(TestCase):
    def test_video_url_is_in_both_public_detail_routes(self):
        course = Course.objects.create(
            title="Course with a preview",
            status=Course.STATUS_PUBLISHED,
            promo_video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

        by_id = self.client.get(f"/api/courses/public/{course.id}/")
        by_slug = self.client.get(f"/api/courses/public/by-slug/{course.slug}/")

        self.assertEqual(by_id.status_code, 200)
        self.assertEqual(by_slug.status_code, 200)
        self.assertEqual(by_id.json()["promo_video_url"], course.promo_video_url)
        self.assertEqual(by_slug.json()["promo_video_url"], course.promo_video_url)
