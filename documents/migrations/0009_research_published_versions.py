from django.db import migrations, models


def backfill(apps, schema_editor):
    Paper = apps.get_model("documents", "ResearchSubmission")
    Paper.objects.filter(document__isnull=False, published_version__isnull=True).update(published_version=models.F("current_version"))


class Migration(migrations.Migration):
    dependencies = [("documents", "0008_researchcase_and_more")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
