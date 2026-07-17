"""Seed the Explore-library RBAC permissions and grant them to MODERATOR/ADMIN.

Mirrors 0027_seed_rbac.py — idempotent (safe to re-run). The seeded MODERATOR
role gains all `documents.*` permissions so documents.permissions.
IsDocumentsModerator (staff / documents.moderate / MODERATOR role) converges.
"""
from django.db import migrations


# (codename, display name, category)
PERMISSIONS = [
    ("documents.moderate", "Access Explore moderation panel", "Explore"),
    ("documents.reports.view", "View reported documents", "Explore"),
    ("documents.reports.action", "Act on document reports", "Explore"),
    ("documents.duplicates.review", "Review the duplicate-uploads queue", "Explore"),
    ("documents.uploaders.warn", "Warn uploaders", "Explore"),
    ("documents.uploaders.suspend", "Suspend uploaders", "Explore"),
    ("documents.uploaders.ban", "Ban uploaders", "Explore"),
    ("documents.manage", "Remove / restore documents", "Explore"),
    ("documents.categories.manage", "Manage document categories", "Explore"),
    ("documents.analytics.view", "View Explore moderation analytics", "Explore"),
]

DOCS_PERMS = [c for (c, _, _) in PERMISSIONS]

# role name -> list of permission codenames (both moderator + admin get all)
ROLE_MAP = {
    "MODERATOR": DOCS_PERMS,
    "ADMIN": DOCS_PERMS,
}


def seed(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    perm_by_code = {}
    for codename, name, category in PERMISSIONS:
        perm, _ = Permission.objects.get_or_create(
            codename=codename,
            defaults={"name": name, "category": category},
        )
        if perm.name != name or perm.category != category:
            perm.name = name
            perm.category = category
            perm.save(update_fields=["name", "category"])
        perm_by_code[codename] = perm

    for role_name, codenames in ROLE_MAP.items():
        role, _ = Role.objects.get_or_create(name=role_name)
        for codename in codenames:
            RolePermission.objects.get_or_create(
                role=role, permission=perm_by_code[codename]
            )


def unseed(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    RolePermission = apps.get_model("accounts", "RolePermission")
    RolePermission.objects.filter(permission__codename__in=DOCS_PERMS).delete()
    Permission.objects.filter(codename__in=DOCS_PERMS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0027_seed_rbac"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
