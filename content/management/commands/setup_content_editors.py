# PLACEMENT: backend/content/management/commands/setup_content_editors.py
#
# Creates a "Content Editors" auth group with CRUD on the content models,
# scoped per-model rather than blanket add/change/delete/view on all of
# them — a couple of models have row sets that are fixed or append-only
# and must not be freely creatable/deletable/rewritable by editorial staff.
# Add editorial staff users to this group and mark them is_staff — they get
# exactly the CMS section of the admin.
#
#     python manage.py setup_content_editors

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db.models import Q

from content import models as m

GROUP_NAME = "Content Editors"

# Full add/change/delete/view — ordinary editorial content.
FULL_CRUD_MODELS = (
    m.BlogPost, m.CurrentAffair, m.FAQItem, m.Announcement,
    m.ShowcaseCourse, m.ContentTag,
    m.HomeContentBlock, m.HomeListItem, m.HomeFloater,
    # Editor-uploaded rich-text images (the media library) — full CRUD.
    m.ContentImage,
)

# Change + view only, no add/delete: the row set is fixed, not editor-owned.
#   - HomeSectionOrder: one row per HomeSection, seeded once by migration
#     0008 and never freely created/removed afterwards. Its own admin API
#     (HomeSectionOrderAdminViewSet, content/admin_views.py) is deliberately
#     List+Update only (no create/destroy) for the same reason — mirror
#     that restriction here instead of granting a permission the API
#     doesn't even expose a way to exercise.
CHANGE_ONLY_MODELS = (
    m.HomeSectionOrder,
)

# View only: append-only audit trail, must never be editable.
#   - BlogRevision: a snapshot of a BlogPost's body taken automatically
#     before every admin overwrite (see its docstring in content/models.py)
#     — the real undo path for the block editor. It has no
#     retention/pruning policy by design. Granting add/change/delete would
#     let an editor rewrite or erase history they're supposed to be
#     accountable to; view-only lets them consult it without that risk.
VIEW_ONLY_MODELS = (
    m.BlogRevision,
)

ACTIONS_BY_MODEL = {}
ACTIONS_BY_MODEL.update({model: ("add", "change", "delete", "view") for model in FULL_CRUD_MODELS})
ACTIONS_BY_MODEL.update({model: ("change", "view") for model in CHANGE_ONLY_MODELS})
ACTIONS_BY_MODEL.update({model: ("view",) for model in VIEW_ONLY_MODELS})


class Command(BaseCommand):
    help = f'Create/refresh the "{GROUP_NAME}" group with content-app permissions.'

    def handle(self, *args, **options):
        group, created = Group.objects.get_or_create(name=GROUP_NAME)

        content_types = ContentType.objects.get_for_models(*ACTIONS_BY_MODEL.keys())

        query = Q()
        for model, actions in ACTIONS_BY_MODEL.items():
            ct = content_types[model]
            codenames = [f"{action}_{model._meta.model_name}" for action in actions]
            query |= Q(content_type=ct, codename__in=codenames)

        perms = Permission.objects.filter(query)
        group.permissions.set(perms)
        self.stdout.write(self.style.SUCCESS(
            f'{"Created" if created else "Updated"} group "{GROUP_NAME}" '
            f"with {perms.count()} permissions. "
            "Add editors to it (and tick is_staff) in the Users admin."
        ))
