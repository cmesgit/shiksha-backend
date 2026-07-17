"""Seed the RBAC baseline: permissions, the MODERATOR/ADMIN roles, and the
default role→permission mappings. Idempotent (safe to re-run)."""
from django.db import migrations


# (codename, display name, category)
PERMISSIONS = [
    # Forum moderation
    ("forum.moderate", "Access moderation panel", "Forum"),
    ("forum.reports.view", "View reported content", "Forum"),
    ("forum.reports.action", "Act on reports (dismiss/delete)", "Forum"),
    ("forum.autorejected.review", "Review auto-rejected queue", "Forum"),
    ("forum.users.warn", "Warn forum users", "Forum"),
    ("forum.users.suspend", "Suspend forum users", "Forum"),
    ("forum.users.ban", "Ban forum users", "Forum"),
    ("forum.threads.manage", "Lock / unlock / remove threads", "Forum"),
    ("forum.categories.manage", "Manage forum categories", "Forum"),
    ("forum.analytics.view", "View moderation analytics", "Forum"),
    # Role administration
    ("roles.view", "View roles & permissions", "Roles"),
    ("roles.manage", "Create / edit roles & permissions", "Roles"),
    ("roles.assign", "Assign / revoke user roles", "Roles"),
]

FORUM_PERMS = [c for (c, _, cat) in PERMISSIONS if cat == "Forum"]
ALL_PERMS = [c for (c, _, _) in PERMISSIONS]

# role name -> list of permission codenames
ROLE_MAP = {
    "MODERATOR": FORUM_PERMS,
    "ADMIN": ALL_PERMS,
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
        # Keep display metadata fresh on re-run.
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
    RolePermission.objects.filter(
        permission__codename__in=ALL_PERMS
    ).delete()
    Permission.objects.filter(codename__in=ALL_PERMS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0026_permission_rolepermission"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
