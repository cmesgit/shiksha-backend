# Generated for multi-subject expert teaching.
# An expert can now teach more than one subject: `categories` holds the full
# set; the old `category` FK remains as the primary subject (synced to the
# first entry) for backward compatibility.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("skills", "0010_expertprofile_is_suspended"),
    ]

    operations = [
        migrations.AddField(
            model_name="expertprofile",
            name="categories",
            field=models.ManyToManyField(
                blank=True, related_name="multi_experts", to="skills.skillcategory"
            ),
        ),
    ]
