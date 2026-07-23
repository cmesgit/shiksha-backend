"""
Encrypted-at-rest field for secrets stored in a DB row (currently just
GlobalSettings.razorpay_key_secret — was plaintext CharField).

Transparent to callers: application code reads/writes the plain secret as a
normal Python str; only the DB column holds ciphertext.
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)


def _fernet():
    # Derive a stable 32-byte urlsafe-base64 Fernet key from a dedicated
    # FIELD_ENCRYPTION_KEY setting if one is configured, else from SECRET_KEY
    # — so this works out of the box (dev/tests) with zero new required
    # config, while production can (and should) set FIELD_ENCRYPTION_KEY
    # separately so rotating SECRET_KEY doesn't also break decryption of
    # already-stored secrets.
    raw = getattr(settings, "FIELD_ENCRYPTION_KEY", None) or settings.SECRET_KEY
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


class EncryptedCharField(models.CharField):
    """CharField encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256).

    An empty string is stored (and read back) as an empty string — never
    encrypted — so `bool(value)` checks like `razorpay_secret_set` keep
    working unchanged on the decrypted Python value.

    Legacy rows written before this field existed hold plaintext. On read, a
    value that doesn't decrypt as a Fernet token is assumed to be that legacy
    plaintext and returned as-is (logged, not raised) — the next save()
    re-encrypts it. `manage.py encrypt_legacy_secrets` forces this once.
    """

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        return _fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        try:
            return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken:
            logger.warning(
                "%s: stored value is not a valid Fernet token — treating as "
                "legacy plaintext (will be encrypted on next save).",
                self.attname,
            )
            return value
        except Exception:
            logger.exception("%s: failed to decrypt stored value", self.attname)
            return value
