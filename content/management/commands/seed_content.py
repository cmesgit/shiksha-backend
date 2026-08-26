# PLACEMENT: backend/content/management/commands/seed_content.py
#
# Demo rows for local development / staging demos:
#     python manage.py seed_content
# Idempotent — safe to run repeatedly (keyed on slugs/questions).

from django.core.management.base import BaseCommand
from django.utils import timezone

from content.models import (
    Announcement, BlogPost, ContentTag, CurrentAffair, FAQItem,
    PublishStatus, ShowcaseCourse,
)


class Command(BaseCommand):
    help = "Seed demo content (blogs, current affairs, FAQs, announcement, showcase)."

    def handle(self, *args, **options):
        ncert, _ = ContentTag.objects.get_or_create(name="NCERT")
        boards, _ = ContentTag.objects.get_or_create(name="Board Exam")

        post, created = BlogPost.objects.get_or_create(
            slug="class-9/economics/chapter-1",
            defaults=dict(
                title="Chapter 1: The Story of Village Palampur",
                class_level="9",
                subject="economics",
                chapter_number=1,
                excerpt=(
                    "How land, labour and capital come together in a farming "
                    "village — the foundation chapter of Class 9 Economics."
                ),
                body_html=(
                    "<h1>The Story of Village Palampur</h1>"
                    "<p>Palampur is a hypothetical village used by NCERT to "
                    "introduce the basics of production: land, labour, "
                    "physical capital and human capital.</p>"
                    "<h2>Key ideas</h2>"
                    "<ul><li>Factors of production</li>"
                    "<li>Multiple cropping and modern farming methods</li>"
                    "<li>Non-farm activities in villages</li></ul>"
                ),
                status=PublishStatus.PUBLISHED,
                publish_at=timezone.now(),
                is_featured=True,
            ),
        )
        if created:
            post.tags.add(ncert, boards)

        CurrentAffair.objects.get_or_create(
            slug="union-budget-highlights-demo",
            defaults=dict(
                title="Union Budget: key highlights for students",
                affair_date=timezone.localdate(),
                category="economy",
                summary=(
                    "A quick, exam-oriented summary of this year's budget "
                    "announcements relevant to the Indian economy syllabus."
                ),
                body_html="<p>Demo body — replace with real coverage.</p>",
                source_name="PIB",
                status=PublishStatus.PUBLISHED,
                publish_at=timezone.now(),
            ),
        )

        for i, (q, a) in enumerate([
            ("How do I enroll in a course?",
             "<p>Create a free account, choose your program and enroll in a "
             "few steps. Guest Preview lets you explore first.</p>"),
            ("Are live classes recorded?",
             "<p>Yes — every live class is recorded and added to your "
             "dashboard for revision.</p>"),
        ]):
            FAQItem.objects.get_or_create(
                page="home", question=q,
                defaults=dict(answer_html=a, order=i, status='published'),
            )

        Announcement.objects.get_or_create(
            message="Admissions open for the new academic session — enroll today!",
            defaults=dict(link_url="/courses", link_label="Browse courses",
                          level="info", status='published'),
        )

        ShowcaseCourse.objects.get_or_create(
            title="Class 10 Foundation",
            defaults=dict(
                level_label="Foundation", ribbon="Bestseller",
                price_label="1,500",
                categories=["class8-12"],
                gradient_css="rgba(255,178,29,0.72),rgba(242,140,15,0.88)",
                image_url=(
                    "https://images.unsplash.com/photo-1434030216411-0b793f4b4173"
                    "?w=800&h=400&fit=crop&auto=format&q=75"
                ),
                icon="book", link_path="/courses",
                link_state={"selectedBoardGroup": "central",
                            "selectedBoard": "cbse"},
                order=0, status='published',
            ),
        )

        self.stdout.write(self.style.SUCCESS("Demo content seeded."))
