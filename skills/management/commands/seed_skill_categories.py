"""
skills/management/commands/seed_skill_categories.py

Run once after deploying:
    python manage.py seed_skill_categories

Safe to re-run — uses get_or_create, won't duplicate.
"""
from django.core.management.base import BaseCommand
from skills.models import SkillCategory


CATEGORIES = [
    {"slug": "coding",   "label": "Coding & Web",        "icon": "▣", "color": "#1b9c85", "order": 1},
    {"slug": "design",   "label": "Design & Art",         "icon": "✦", "color": "#a78bfa", "order": 2},
    {"slug": "music",    "label": "Music & Audio",        "icon": "♪", "color": "#ff8f01", "order": 3},
    {"slug": "lang",     "label": "Languages",            "icon": "ᴬ", "color": "#60a5fa", "order": 4},
    {"slug": "business", "label": "Business & Finance",   "icon": "₹", "color": "#f87171", "order": 5},
    {"slug": "exam",     "label": "Exam Prep",            "icon": "✎", "color": "#125027", "order": 6},
    {"slug": "crafts",   "label": "Crafts & Handmade",    "icon": "✄", "color": "#d97757", "order": 7},
    {"slug": "spoken",   "label": "Public Speaking",      "icon": "❝", "color": "#1dcaab", "order": 8},
]


class Command(BaseCommand):
    help = "Seed the 8 default SkillCategory rows. Safe to re-run."

    def handle(self, *args, **options):
        created_count = 0
        for cat in CATEGORIES:
            obj, created = SkillCategory.objects.get_or_create(
                slug=cat["slug"],
                defaults={
                    "label":    cat["label"],
                    "icon":     cat["icon"],
                    "color":    cat["color"],
                    "order":    cat["order"],
                    "is_active": True,
                },
            )
            if not created:
                # Update label/icon/color in case they changed
                obj.label    = cat["label"]
                obj.icon     = cat["icon"]
                obj.color    = cat["color"]
                obj.order    = cat["order"]
                obj.is_active = True
                obj.save(update_fields=["label", "icon", "color", "order", "is_active"])
                self.stdout.write(f"  updated  {cat['slug']}")
            else:
                created_count += 1
                self.stdout.write(f"  created  {cat['slug']}")

        # Remove the garbage "sdsd" entry if it exists
        deleted, _ = SkillCategory.objects.exclude(
            slug__in=[c["slug"] for c in CATEGORIES]
        ).delete()
        if deleted:
            self.stdout.write(f"  cleaned up {deleted} unknown categor{'y' if deleted == 1 else 'ies'}")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {created_count} created, {len(CATEGORIES) - created_count} updated."
        ))
