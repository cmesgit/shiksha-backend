import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0018_merge_20260630_1651"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgreementLetter",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("key", models.SlugField(max_length=50, unique=True)),
                ("title", models.CharField(max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="AgreementLetterVersion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version_number", models.PositiveIntegerField()),
                ("title", models.CharField(max_length=200)),
                ("body", models.TextField()),
                ("change_note", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("letter", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="versions", to="accounts.agreementletter")),
            ],
            options={
                "ordering": ["-version_number"],
                "unique_together": {("letter", "version_number")},
            },
        ),
        migrations.AddField(
            model_name="agreementletter",
            name="current_version",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="accounts.agreementletterversion"),
        ),
        migrations.AddIndex(
            model_name="agreementletterversion",
            index=models.Index(fields=["letter", "version_number"], name="accounts_ag_letter__idx"),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="signed_agreement_version",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="signed_by", to="accounts.agreementletterversion"),
        ),
    ]
