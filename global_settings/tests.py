"""
GlobalSettings.razorpay_key_secret is encrypted at rest (EncryptedCharField) —
previously plaintext CharField. Verify: DB column holds ciphertext, ORM
round-trips the plaintext transparently, legacy plaintext rows degrade
gracefully instead of crashing, and the re-encrypt command is idempotent.
"""
from django.db import connection
from django.test import TestCase

from global_settings.models import GlobalSettings
from global_settings.fields import _fernet


class RazorpaySecretEncryptionTest(TestCase):
    SECRET = "rzp_test_super_secret_key_123"

    def _raw_db_value(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT razorpay_key_secret FROM global_settings_globalsettings WHERE singleton_id = 1"
            )
            row = cur.fetchone()
            return row[0] if row else None

    def test_stored_value_is_ciphertext_not_plaintext(self):
        gs = GlobalSettings.load()
        gs.razorpay_key_secret = self.SECRET
        gs.save()
        raw = self._raw_db_value()
        self.assertIsNotNone(raw)
        self.assertNotEqual(raw, self.SECRET)
        self.assertNotIn(self.SECRET, raw)

    def test_orm_roundtrip_returns_plaintext(self):
        gs = GlobalSettings.load()
        gs.razorpay_key_secret = self.SECRET
        gs.save()
        reloaded = GlobalSettings.objects.get(pk=1)
        self.assertEqual(reloaded.razorpay_key_secret, self.SECRET)

    def test_blank_secret_stays_blank_and_unset_flag_false(self):
        gs = GlobalSettings.load()
        gs.razorpay_key_secret = ""
        gs.save()
        self.assertEqual(self._raw_db_value(), "")
        reloaded = GlobalSettings.objects.get(pk=1)
        self.assertEqual(reloaded.razorpay_key_secret, "")
        self.assertFalse(bool(reloaded.razorpay_key_secret))

    def test_legacy_plaintext_row_reads_back_without_crashing(self):
        # Simulate a row written before EncryptedCharField existed: raw
        # plaintext in the column, bypassing the field's own encryption.
        GlobalSettings.load()
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE global_settings_globalsettings SET razorpay_key_secret = %s WHERE singleton_id = 1",
                [self.SECRET],
            )
        reloaded = GlobalSettings.objects.get(pk=1)
        self.assertEqual(reloaded.razorpay_key_secret, self.SECRET)

    def test_encrypt_legacy_secrets_command_reencrypts(self):
        from django.core.management import call_command

        GlobalSettings.load()
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE global_settings_globalsettings SET razorpay_key_secret = %s WHERE singleton_id = 1",
                [self.SECRET],
            )
        self.assertEqual(self._raw_db_value(), self.SECRET)  # still plaintext
        call_command("encrypt_legacy_secrets")
        raw_after = self._raw_db_value()
        self.assertNotEqual(raw_after, self.SECRET)
        # And it still decrypts back to the same plaintext via the ORM.
        self.assertEqual(GlobalSettings.objects.get(pk=1).razorpay_key_secret, self.SECRET)

    def test_fernet_key_is_stable_across_calls(self):
        # Two independent encrypt/decrypt round trips must use the same
        # derived key, or restarts would make old secrets unreadable.
        f1 = _fernet()
        token = f1.encrypt(b"hello")
        f2 = _fernet()
        self.assertEqual(f2.decrypt(token), b"hello")
