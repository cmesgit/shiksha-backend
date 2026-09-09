"""Turn the DigiLocker verification method off — it can never complete.

`allow_digilocker` defaulted True, and `Verify.jsx` reads the method list
from `GET /api/scholarship/config/`, labels DigiLocker "Recommended" and
auto-selects it. But `GuardianVerificationCreateView` only ever creates a
`pending` record for that method: there is no OAuth2 callback route, nothing
ever moves the record to `verified`, and the screen polls a status that will
never change. That was live on prod, on the first screen of the scholarship
funnel that asks for anything.

⚠ THE `AlterField` ALONE WOULD DO NOTHING ON ANY DEPLOYED SYSTEM.
A model default applies when a row is CREATED, and `ScholarshipSettings` is a
singleton whose row already exists with `allow_digilocker=True`. This repo has
been bitten by exactly that before — see the "changing a model field's
default does NOT retroactively update existing rows" note in
memory/instant-scholarship-module-scoping.md. So this migration writes the
existing row too; without the second operation the fix would apply to a fresh
database and to nothing else.

Forcing the row to False is correct *here specifically* because the method has
never worked: every True in existence is "nobody noticed", not "an admin
deliberately enabled this". Manual document review stays on and remains what
it already was — the only path that can actually reach `verified` today.
Aadhaar offline e-KYC is untouched and also still works.

Re-enabling means building the Meri Pehchaan callback first, and clearing the
org-onboarding gate described on the model field.
"""
from django.db import migrations, models


def turn_off(apps, schema_editor):
    ScholarshipSettings = apps.get_model("scholarship", "ScholarshipSettings")
    ScholarshipSettings.objects.update(allow_digilocker=False)


def noop(apps, schema_editor):
    """Reversing restores the DEFAULT but deliberately not the row.

    Rolling back the code should not silently re-offer a dead-end method to
    parents mid-funnel. If someone genuinely wants it back, that is one click
    in admin settings — and it still will not complete.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("scholarship", "0002_scholarshipsettings_allow_aadhaar_offline_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scholarshipsettings",
            name="allow_digilocker",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="scholarshipsettings",
            name="active_kyc_provider",
            field=models.CharField(
                blank=True,
                max_length=30,
                help_text=(
                    "Reseller handling Aadhaar OTP calls (e.g. setu, digio, "
                    "surepass, hyperverge). ShikshaCom must never call UIDAI "
                    "directly or store an Aadhaar number/hash — only the "
                    "reseller's opaque, non-reversible verification reference. "
                    "NOTE: this does NOT apply to DigiLocker, which a company "
                    "integrates with directly via API Setu / Meri Pehchaan and "
                    "needs no reseller and no AUA/KUA licence — an earlier "
                    "version of this text claimed otherwise."
                ),
            ),
        ),
        migrations.RunPython(turn_off, noop),
    ]
