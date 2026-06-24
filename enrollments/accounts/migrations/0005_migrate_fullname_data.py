from django.db import migrations


def split_full_name(apps, schema_editor):
    """Split existing full_name into first_name + last_name."""
    Profile = apps.get_model("accounts", "Profile")
    for profile in Profile.objects.exclude(full_name__isnull=True).exclude(full_name=""):
        parts = profile.full_name.strip().split(" ", 1)
        profile.first_name = parts[0]
        profile.last_name = parts[1] if len(parts) > 1 else ""
        profile.save(update_fields=["first_name", "last_name"])


def reverse_split(apps, schema_editor):
    """Reverse: merge first_name + last_name back to full_name."""
    Profile = apps.get_model("accounts", "Profile")
    for profile in Profile.objects.all():
        if profile.first_name or profile.last_name:
            profile.full_name = f"{profile.first_name} {profile.last_name}".strip()
            profile.save(update_fields=["full_name"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_form_fillup_revamp"),
    ]

    operations = [
        migrations.RunPython(split_full_name, reverse_split),
    ]
