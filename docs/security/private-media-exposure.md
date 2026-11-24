# Private media is publicly readable on the CDN

**Status: LIVE. Not closed by this patch.** Re-verified against the code on 11 November 2026. No production server was contacted for this write-up.

## What is wrong

`config/bunny_storage.py`'s `url()` returns a bare public CDN URL for every `FileField`. `config/media_security.py` — the deny-by-default module with per-field rules for teacher ID proofs, learner photos, guardian KYC documents and receipts — only runs on the `SecureLocalStorage` path, which production does not use. Grepping for its `is_authorized` outside its own file returns **comments and docstrings only**. It is dead code in production.

Nothing on the storage zone requires a token, so any key that exists is served to anyone who asks for it. The categories affected are the ones that matter: teacher government-ID scans and signed agreements, learner (child) photos, `scholarship.GuardianVerification.manual_document`, payment receipts, assignment submissions, chat attachments, counseling session reports.

Worse than "needs the path": `upload_to` uses flat prefixes and filenames of the form `{profile_id}_{field}.{ext}`, so the namespace is **enumerable** by walking integers.

## What this patch does and does not do

Does: routes every storage URL through one function (`bunny_signing.bunny_storage_url`), adds Bunny's documented token signature, and names the private prefixes in settings so the list is auditable in one place.

**Does not: make anything private.** A signed URL is not a gate. It becomes one only when Bunny is configured to *reject unsigned* requests. Until then this changes nothing a visitor sees, which is deliberate and is pinned by `InertUntilConfiguredTests` — with no `BUNNY_STORAGE_TOKEN_KEY` the output is byte-for-byte what it was.

## The audit that makes a fix feasible

**There is exactly one place a storage URL is constructed** — `config/bunny_storage.py`. All 342 other references go through `.url`. This is the opposite of the Stream-embed situation, where `skills/models.py` and `skills/listing_models.py` build `iframe.mediadelivery.net` URLs by hand, which is why embed token auth still cannot be switched on.

So the signing side is a single, central change. The obstacle is elsewhere.

## The obstacle, and the decision it forces

**A Bunny pull zone serves its entire storage zone.** Token authentication is a pull-zone setting, so it cannot be enabled for `teachers/` while `content/` stays public *on the same zone*. Adding a second, token-protected pull zone does not help either: the original zone keeps serving the same private paths, so the hole stays open.

That leaves two real options, and this is the decision that needs an owner:

**A. Separate storage zones.** Move private prefixes into a second Bunny storage zone whose pull zone requires tokens; public content stays where it is. Correct and durable, and public URLs keep working untouched. Cost: a data migration plus a `FileField` storage split, and every existing private file has to be re-keyed.

**B. Token auth on the existing zone, everything signed.** One dashboard switch, no migration. Breaks every **persisted absolute URL** — `image_url` columns and absolute `<img src>` in rich-text bodies carry no token and would 403 permanently. It also defeats CDN caching for public assets and breaks any externally shared link.

Option A is the recommendation. Option B is faster and is how this gets done badly.

Either way, the `BUNNY_PRIVATE_PREFIXES` list in `settings_base.py` is the inventory to migrate, and it should be reviewed against `upload_to` values before anyone acts on it — it was derived by reading them, not by listing the live zone.

## Order of operations

1. Land this patch. Inert; nothing changes.
2. Decide A or B, with a named owner.
3. For A: create the zone, enable token auth on its pull zone, migrate files, repoint the private `FileField`s, verify old paths 404 on the public zone.
4. Set `BUNNY_STORAGE_TOKEN_KEY` **in staging first** and confirm a signed request is served and an unsigned one is refused. The signature below is unverified until this step passes.
5. Only then production. Keep the key unset to roll back — URLs revert to unsigned automatically, which is the pre-existing behaviour, not a new failure.

## What is unproven

The signature is implemented from Bunny's published formula — `Base64(SHA256_RAW(key + path + expires))`, URL-safe, `?token=&expires=` — and has **never been served by a real Bunny zone**, because no zone has token auth on. A wrong byte fails closed (403), so the failure mode is a broken download rather than an ungated one. Treat step 4 as the proof, not these tests.

Also unaddressed here, from the same exposure: `TeacherProfile.id_number` stores a raw government ID number in plaintext — including Aadhaar, which `scholarship/models.py` explicitly refuses to store, citing the Aadhaar Act. **The two subsystems are governed by opposite rules.** That is a separate fix and signing the file does not touch it.

## Related

`BUNNY_ACCOUNT_API_KEY` is still unset, so CDN purge-on-delete is wired but inert — a deleted file stays readable from edge cache. Private files therefore need *both* this gate and that key.
