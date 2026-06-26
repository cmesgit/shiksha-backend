# PLACEMENT: skills/migrations/0006_expert_location_advertising_subscription.py  (NEW FILE)
# Adds:
#   • ExpertProfile: advertising (is_featured/featured_since/reach_count),
#     offline-class location (class_mode/class_location/pincode/state/district/
#     city/latitude/longitude), teaching extras (languages/subject_description),
#     and direct (P2P) payee details (payment_upi/payment_name/payment_note).
#   • ExpertAdSubscription: the monthly advertising subscription.
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("skills", "0005_remove_skill_messaging"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── ExpertProfile: advertising ──────────────────────────────────────
        migrations.AddField(
            model_name="expertprofile",
            name="is_featured",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="featured_since",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="reach_count",
            field=models.PositiveIntegerField(default=0),
        ),
        # ── ExpertProfile: offline-class location ───────────────────────────
        migrations.AddField(
            model_name="expertprofile",
            name="class_mode",
            field=models.CharField(
                choices=[
                    ("home", "At my place"),
                    ("travel", "I can travel to the learner"),
                    ("online", "Online only"),
                ],
                default="online",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="class_location",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="pincode",
            field=models.CharField(blank=True, max_length=10),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="state",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="district",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="city",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="latitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="longitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        # ── ExpertProfile: teaching extras ──────────────────────────────────
        migrations.AddField(
            model_name="expertprofile",
            name="languages",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="subject_description",
            field=models.TextField(blank=True),
        ),
        # ── ExpertProfile: direct (P2P) payee details ───────────────────────
        migrations.AddField(
            model_name="expertprofile",
            name="payment_upi",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="payment_name",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="expertprofile",
            name="payment_note",
            field=models.CharField(blank=True, max_length=200),
        ),
        # ── ExpertAdSubscription ────────────────────────────────────────────
        migrations.CreateModel(
            name="ExpertAdSubscription",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("plan", models.CharField(
                    choices=[("free", "Free (launch period)"), ("monthly", "Monthly")],
                    default="monthly", max_length=10)),
                ("status", models.CharField(
                    choices=[
                        ("pending", "Pending payment"),
                        ("submitted", "Payment submitted"),
                        ("active", "Active"),
                        ("cancelled", "Cancelled"),
                        ("expired", "Expired"),
                    ],
                    default="pending", max_length=12)),
                ("amount", models.PositiveIntegerField(default=49900, help_text="Paise")),
                ("auto_renew", models.BooleanField(default=True)),
                ("current_period_start", models.DateTimeField(blank=True, null=True)),
                ("current_period_end", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("upi_reference", models.CharField(blank=True, max_length=40)),
                ("payer_vpa", models.CharField(blank=True, max_length=120)),
                ("receipt", models.ImageField(blank=True, null=True, upload_to="skills/ad_subscriptions/receipts/")),
                ("note", models.TextField(blank=True)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("expert", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="ad_subscription", to="skills.expertprofile")),
                ("reviewed_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="reviewed_ad_subscriptions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-updated_at"],
            },
        ),
        migrations.AddIndex(
            model_name="expertadsubscription",
            index=models.Index(fields=["status"], name="skills_expe_status_idx"),
        ),
    ]
