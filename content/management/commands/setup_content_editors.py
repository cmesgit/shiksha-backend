# PLACEMENT: backend/content/management/commands/setup_content_editors.py
#
# Creates a "Content Editors" auth group with full CRUD on every content
# model (and nothing else). Add editorial staff users to this group and
# mark them is_staff — they get exactly the CMS section of the admin.
#
#     python manage.py setup_content_editors

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

from content import models as m

GROUP_NAME = "Content Editors"
MODELS = (
    m.BlogPost, m.CurrentAffair, m.FAQItem, m.Announcement,
    m.ShowcaseCourse, m.ContentTag,
)


class Command(BaseCommand):
    help = f'Create/refresh the "{GROUP_NAME}" group with content-app permissions.'

    def handle(self, *args, **options):
        group, created = Group.objects.get_or_create(name=GROUP_NAME)
        perms = Permission.objects.filter(
            content_type__in=ContentType.objects.get_for_models(*MODELS).values()
        )
        group.permissions.set(perms)
        self.stdout.write(self.style.SUCCESS(
            f'{"Created" if created else "Updated"} group "{GROUP_NAME}" '
            f"with {perms.count()} permissions. "
            "Add editors to it (and tick is_staff) in the Users admin."
        ))
