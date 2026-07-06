# Merges the two skills migration leaves that have existed side-by-side
# since 0006_expert_location_advertising_subscription:
#
#   Leaf A — 0007_skillsession_started_at
#       AddField(SkillSession.started_at)
#
#   Leaf B — 0011_expertprofile_categories
#       (via 0007_merge_20260625_1909 → 0008..0011)
#       AddField/RemoveField on ExpertProfile.availability_slots,
#       ExpertProfile.is_suspended, ExpertProfile.categories
#
# Neither branch touches a field the other one does, so this merge is a
# pure graph join — no operations needed. Run `makemigrations` for skills
# after this and it will proceed normally instead of raising
# "Conflicting migrations detected; multiple leaf nodes".

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("skills", "0007_skillsession_started_at"),
        ("skills", "0011_expertprofile_categories"),
    ]

    operations = [
    ]
