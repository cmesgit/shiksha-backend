"""
Aadhaar Paperless Offline e-KYC verification — the one identity-verification
path in this app that is both genuinely free and legally clean without any
AUA/KUA licence or paid reseller: the resident downloads a share-code-
protected ZIP from UIDAI's own portal and shares it with us; we verify
UIDAI's own digital signature on the XML inside using UIDAI's own published
public key. Nothing is paid for, nothing routes through a third party.

Compliance/security notes — read before touching this file:

- We NEVER extract, log, or persist the Aadhaar number itself, in any form.
  The XML's own `referenceId` attribute embeds the *last 4 digits* of the
  Aadhaar number followed by a generation timestamp (confirmed via UIDAI's
  own FAQ). `dedup_reference_for()` deliberately excludes it from the hash
  it builds — dedup is keyed on verified name+DOB+gender only.
- We NEVER persist the resident's share code beyond the single request that
  needs it to open the ZIP (it is used in-memory and discarded).
- UIDAI's published public certificate
  (`certs/uidai_offline_publickey_26022019.cer`) is EXPIRED — not after
  9 Apr 2019 — and has been for years, independently confirmed via openssl
  against a freshly re-downloaded copy. This is UIDAI's own long-standing
  quirk, not a bug in this code: we therefore verify only the RSA signature
  using the key extracted from that certificate and deliberately skip
  X.509 chain/expiry validation, which would always fail against this
  artifact. If UIDAI ever rotates the certificate, this file's SHA-256
  fingerprint (asserted in scholarship/tests.py) will start failing — that
  is the trigger to re-fetch from UIDAI's FAQ page (URL in that test),
  which has been observed to intermittently redirect during a UIDAI site
  migration; retry rather than assume the certificate was discontinued.
- No official UIDAI reference implementation exists for this verification,
  in any language. This was built from UIDAI's own sample-data page plus
  independent verification of the certificate — NOT a vetted SDK. UIDAI's
  own sample document uses the deprecated `rsa-sha1` signature method; a
  live 2026 document's actual algorithm is unconfirmed, so this code tries
  the secure default method set first and falls back to explicitly
  allowing RSA-SHA1 only if that's what the document declares — it does
  not silently downgrade security for a document that could have used a
  stronger method. Test against a REAL downloaded XML before relying on
  this in production.
"""
import dataclasses
import datetime
import hashlib
import io
import os
import zipfile

from defusedxml import ElementTree as DefusedET
from defusedxml.lxml import fromstring as defused_lxml_fromstring
from signxml import SignatureMethod, XMLVerifier
from signxml.exceptions import InvalidSignature

CERT_PATH = os.path.join(os.path.dirname(__file__), "certs", "uidai_offline_publickey_26022019.cer")

# UIDAI provides no revocation mechanism for this document — once
# downloaded, the signature never expires. Treat an old one as untrustworthy
# for a *new* scholarship attempt rather than accepting it indefinitely.
MAX_DOCUMENT_AGE_DAYS = 30

# A real UIDAI Offline e-KYC XML is a few KB. This is generous headroom, not
# a realistic size — it exists purely to cap the decompression bomb below:
# zf.read() previously decompressed the single member fully into memory with
# NO size check at all, so a crafted ~50MB DEFLATE zip (max ratio ~1032:1)
# could balloon to tens of GB in one request and OOM-kill the worker.
MAX_UNCOMPRESSED_XML_BYTES = 10 * 1024 * 1024  # 10 MB


class AadhaarOfflineVerificationError(Exception):
    """Raised with a message safe to show directly to the end user."""


def _load_uidai_cert_pem():
    with open(CERT_PATH, "rb") as f:
        return f.read()


def _extract_xml_from_zip(zip_bytes, share_code):
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            if len(names) != 1:
                raise AadhaarOfflineVerificationError(
                    "Unexpected contents — the Offline e-KYC ZIP should contain exactly one file."
                )
            zf.setpassword(share_code.encode("utf-8"))
            # zf.read(name) decompresses the WHOLE member into memory in one
            # call — and the member's declared uncompressed-size header is
            # attacker-controlled and only checked against the real output
            # via a CRC comparison at the very end, after decompression
            # already happened. So checking that header first (info.file_size)
            # is not a real defense: a crafted entry can declare a small size
            # while its compressed bytes still inflate to gigabytes before
            # the mismatch is ever noticed. Read in bounded chunks instead and
            # abort mid-stream the moment actual output exceeds the cap —
            # this bounds real memory use regardless of what any header claims.
            with zf.open(names[0]) as member:
                chunks = []
                total = 0
                while True:
                    chunk = member.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UNCOMPRESSED_XML_BYTES:
                        raise AadhaarOfflineVerificationError(
                            "This file is far larger than a real Offline e-KYC document — rejected."
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
    except RuntimeError as exc:
        # zipfile raises a plain RuntimeError("Bad password for file ...")
        # on a wrong password — there is no dedicated exception for it.
        raise AadhaarOfflineVerificationError(
            "Incorrect share code, or this isn't a valid Offline e-KYC ZIP."
        ) from exc
    except zipfile.BadZipFile as exc:
        raise AadhaarOfflineVerificationError("That doesn't look like a valid ZIP file.") from exc
    except (MemoryError, zipfile.LargeZipFile, NotImplementedError) as exc:
        # Previously uncaught here — a corrupt/hostile archive (unsupported
        # compression method, a size lie the size check above didn't catch
        # because it targets the declared header, not actual decompression)
        # 500'd instead of returning the same clean 400 every other bad-input
        # path in this function does.
        raise AadhaarOfflineVerificationError("That doesn't look like a valid ZIP file.") from exc


def _parse_and_extract_fields(xml_bytes):
    # defusedxml guards this user-supplied XML against XXE/entity-expansion
    # attacks before anything else touches it.
    try:
        root = DefusedET.fromstring(xml_bytes)
    except Exception as exc:
        raise AadhaarOfflineVerificationError("Couldn't read the e-KYC XML file.") from exc

    if root.tag != "OfflinePaperlessKyc":
        raise AadhaarOfflineVerificationError("Not a recognised Aadhaar Offline e-KYC document.")

    reference_id = root.get("referenceId", "")
    uid_data = root.find("UidData")
    poi = uid_data.find("Poi") if uid_data is not None else None
    if poi is None:
        raise AadhaarOfflineVerificationError("Missing identity data in the e-KYC document.")

    return {
        "name": poi.get("name", ""),
        "dob": poi.get("dob") or poi.get("yob") or "",
        "gender": poi.get("gender", ""),
        "reference_id": reference_id,
    }


def _check_freshness(reference_id):
    # referenceId = <last-4-digits-of-Aadhaar><17-digit timestamp
    # YYYYMMDDHHMMSSfff>, per UIDAI's own FAQ. Only the timestamp portion is
    # used here; the Aadhaar-derived prefix is read past, never stored.
    try:
        generated_at = datetime.datetime.strptime(reference_id[4:18], "%Y%m%d%H%M%S")
    except (ValueError, IndexError):
        raise AadhaarOfflineVerificationError("Couldn't read the document's generation timestamp.")

    age_days = (datetime.datetime.now() - generated_at).days
    if age_days > MAX_DOCUMENT_AGE_DAYS or age_days < 0:
        raise AadhaarOfflineVerificationError(
            f"This e-KYC document is more than {MAX_DOCUMENT_AGE_DAYS} days old (or has an invalid "
            "timestamp) — please download a fresh one from the UIDAI portal."
        )


def _verify_signature(xml_bytes):
    lxml_root = defused_lxml_fromstring(xml_bytes)
    cert_pem = _load_uidai_cert_pem()

    try:
        # Pin to UIDAI's own published key rather than trusting whatever
        # certificate is embedded in the document's own KeyInfo block —
        # "does the signature match a cert embedded in the same document"
        # is not a security boundary on its own; anyone could swap both.
        XMLVerifier().verify(lxml_root, x509_cert=cert_pem)
        return
    except InvalidSignature:
        pass  # fall through to the RSA-SHA1 retry below
    except Exception as exc:
        raise AadhaarOfflineVerificationError("Couldn't verify this document's signature.") from exc

    # UIDAI's own published sample document uses the deprecated RSA-SHA1
    # method, which signxml excludes from its secure-by-default method set.
    # Retry once, explicitly widened to allow it — this does not weaken
    # anything we control: UIDAI chose this algorithm for documents it
    # issues, not us for documents we create, and rejecting a genuinely
    # UIDAI-signed document outright would make this verification path
    # non-functional against real, unmodified UIDAI output.
    from signxml import SignatureConfiguration
    default_config = SignatureConfiguration()
    sha1_config = dataclasses.replace(
        default_config, signature_methods=default_config.signature_methods | {SignatureMethod.RSA_SHA1}
    )
    try:
        XMLVerifier().verify(lxml_root, x509_cert=cert_pem, expect_config=sha1_config)
    except InvalidSignature as exc:
        raise AadhaarOfflineVerificationError(
            "This document's digital signature could not be verified — it may have been altered, "
            "or may not be a genuine UIDAI-issued file."
        ) from exc
    except Exception as exc:
        raise AadhaarOfflineVerificationError("Couldn't verify this document's signature.") from exc


def verify_offline_ekyc(zip_bytes, share_code):
    """Verify a UIDAI Offline Paperless e-KYC ZIP end to end. Returns the
    verified {name, dob, gender, reference_id} dict on success; raises
    AadhaarOfflineVerificationError with a user-safe message otherwise."""
    xml_bytes = _extract_xml_from_zip(zip_bytes, share_code)
    fields = _parse_and_extract_fields(xml_bytes)
    _check_freshness(fields["reference_id"])
    _verify_signature(xml_bytes)
    return fields


def dedup_reference_for(verified_fields):
    """A stable reference for eligibility dedup, built only from verified
    name/DOB/gender — deliberately excludes reference_id, which embeds
    Aadhaar-number digits."""
    payload = "|".join([
        verified_fields.get("name", "").strip().lower(),
        verified_fields.get("dob", ""),
        verified_fields.get("gender", "").strip().lower(),
    ])
    return "aadhaar_offline:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
