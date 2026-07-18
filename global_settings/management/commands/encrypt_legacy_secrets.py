"""
One-time re-save of GlobalSettings so any legacy PLAINTEXT razorpay_key_secret
(written before EncryptedCharField existed) gets encrypted at rest.

EncryptedCharField already handles legacy plaintext gracefully on read (see
global_settings/fields.py), so this command is not required for correctness —
it just forces the re-encryption immediately instead of waiting for the next
admin save. Safe to run any time, including when the secret is already
encrypted or blank (no-op).

    python manage.py encrypt_legacy_secrets
"""
from django.core.management.base import BaseCommand

from global_settings.models import GlobalSettings


class Command(BaseCommand):
    help = "Force-encrypt GlobalSettings.razorpay_key_secret if it is still legacy plaintext."

    def handle(self, *args, **opts):
        settings_row = GlobalSettings.load()
        if not settings_row.razorpay_key_secret:
            self.stdout.write("No razorpay_key_secret set — nothing to do.")
            return
        # Re-saving re-runs get_prep_value(), which always encrypts on write —
        # so this is idempotent whether the stored value was legacy plaintext
        # or already ciphertext.
        settings_row.save(update_fields=["razorpay_key_secret"])
        self.stdout.write(self.style.SUCCESS("razorpay_key_secret re-encrypted."))
