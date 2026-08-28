"""LIVEKIT_EGRESS_ENABLED / BUNNY_EGRESS_* settings guards.

Phase 0 of automatic class recording rests on exactly two safety properties,
both computed at settings-import time in config/settings_base.py:

  1. Egress stays OFF unless it was explicitly switched on AND every
     credential it needs is present. A half-configured deploy (or the test
     settings, which set no LIVEKIT_* at all) must keep the existing
     manual-upload behaviour rather than raising on every teacher join.
  2. The egress Storage Zone can never be the CMS Storage Zone. That one is
     shared between dev and prod, so the collision would publish dev's test
     recordings on prod's public CDN — silently, and irreversibly by the time
     anyone noticed.

Both are asserted here by re-importing the settings module under a patched
environment, because that is where the logic actually lives; asserting on the
already-imported `django.conf.settings` would only re-read this sandbox's own
configuration and prove nothing.
"""
import importlib
import os
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

BASE = "config.settings_base"

# Everything the egress path needs, as env vars. Individual tests drop one
# key at a time to prove the readiness check is actually conjunctive.
FULL_ENV = {
    "LIVEKIT_URL": "wss://example.livekit.cloud",
    "LIVEKIT_API_KEY": "key",
    "LIVEKIT_API_SECRET": "secret",
    "BUNNY_EGRESS_ZONE": "shiksha-class-egress",
    "BUNNY_EGRESS_API_KEY": "zone-password",
    "LIVEKIT_EGRESS_ENABLED": "true",
}


def _reload(env):
    """Re-execute settings_base with exactly `env` overlaid, and hand back the
    module. clear=False so BASE_DIR/SECRET_KEY resolution keeps working."""
    with patch.dict(os.environ, env, clear=False):
        return importlib.reload(importlib.import_module(BASE))


class EgressReadinessTest(SimpleTestCase):

    def test_enabled_when_flag_and_all_credentials_present(self):
        mod = _reload(FULL_ENV)
        self.assertTrue(mod.LIVEKIT_EGRESS_ENABLED)

    def test_disabled_when_flag_is_absent_even_with_credentials(self):
        env = {**FULL_ENV}
        env.pop("LIVEKIT_EGRESS_ENABLED")
        env["LIVEKIT_EGRESS_ENABLED"] = ""
        mod = _reload(env)
        self.assertFalse(mod.LIVEKIT_EGRESS_ENABLED)

    def test_disabled_and_warns_when_flag_set_but_credentials_missing(self):
        """The important half. Turning the flag on with an incomplete config
        must not arm a code path that will throw on every teacher join."""
        for missing in (
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
            "BUNNY_EGRESS_ZONE",
            "BUNNY_EGRESS_API_KEY",
        ):
            with self.subTest(missing=missing):
                env = {**FULL_ENV, missing: ""}
                with self.assertWarns(RuntimeWarning):
                    mod = _reload(env)
                self.assertFalse(mod.LIVEKIT_EGRESS_ENABLED)

    def test_flag_accepts_the_usual_truthy_spellings(self):
        for value in ("1", "true", "TRUE", "yes", "on", " true "):
            with self.subTest(value=value):
                mod = _reload({**FULL_ENV, "LIVEKIT_EGRESS_ENABLED": value})
                self.assertTrue(mod.LIVEKIT_EGRESS_ENABLED)

    def test_flag_rejects_other_values(self):
        for value in ("0", "false", "no", "off", "maybe"):
            with self.subTest(value=value):
                mod = _reload({**FULL_ENV, "LIVEKIT_EGRESS_ENABLED": value})
                self.assertFalse(mod.LIVEKIT_EGRESS_ENABLED)



class EgressEndpointTest(SimpleTestCase):
    """The S3 endpoint is derived from the region rather than configured
    separately, because they are the same fact twice and a mismatch shows up
    only as an opaque SigV4 signature error on the first real recording.

    Worth pinning precisely because it was got wrong first time round: Bunny's
    S3-compatible API answers on "<region>-s3.storage.bunnycdn.com", which is
    NOT the native Edge Storage host (storage.bunnycdn.com) that
    config/bunny_storage.py uses for CMS images.
    """

    def test_host_defaults_to_the_frankfurt_s3_endpoint(self):
        mod = _reload(FULL_ENV)
        self.assertEqual(mod.BUNNY_EGRESS_REGION, "de")
        self.assertEqual(
            mod.BUNNY_EGRESS_S3_HOST, "de-s3.storage.bunnycdn.com",
        )

    def test_host_follows_the_configured_region(self):
        for region in ("de", "ny", "sg", "uk", "se", "la", "jh", "syd"):
            with self.subTest(region=region):
                mod = _reload({**FULL_ENV, "BUNNY_EGRESS_REGION": region})
                self.assertEqual(
                    mod.BUNNY_EGRESS_S3_HOST,
                    f"{region}-s3.storage.bunnycdn.com",
                )

    def test_region_is_lowercased_for_sigv4(self):
        """Bunny's own APIs disagree on casing: the zone-creation API and the
        dashboard both say "SG", the S3 API signs with "sg". Pasting the
        dashboard's value must not break signing."""
        mod = _reload({**FULL_ENV, "BUNNY_EGRESS_REGION": "SG"})
        self.assertEqual(mod.BUNNY_EGRESS_REGION, "sg")
        self.assertEqual(
            mod.BUNNY_EGRESS_S3_HOST, "sg-s3.storage.bunnycdn.com",
        )

    def test_region_tolerates_stray_whitespace(self):
        mod = _reload({**FULL_ENV, "BUNNY_EGRESS_REGION": " sg\n"})
        self.assertEqual(mod.BUNNY_EGRESS_REGION, "sg")
        self.assertEqual(
            mod.BUNNY_EGRESS_S3_HOST, "sg-s3.storage.bunnycdn.com",
        )

    def test_explicit_host_overrides_the_derived_one(self):
        mod = _reload({
            **FULL_ENV,
            "BUNNY_EGRESS_REGION": "ny",
            "BUNNY_EGRESS_S3_HOST": "custom-s3.example.net",
        })
        self.assertEqual(mod.BUNNY_EGRESS_S3_HOST, "custom-s3.example.net")

    def test_native_storage_host_has_no_prefix_for_the_main_region(self):
        """Bunny's native host pattern gives de no prefix and every other
        region one — the opposite of the S3 host, which always has one."""
        mod = _reload({**FULL_ENV, "BUNNY_EGRESS_REGION": "de"})
        self.assertEqual(mod.BUNNY_EGRESS_STORAGE_HOST, "storage.bunnycdn.com")

    def test_native_storage_host_is_prefixed_for_other_regions(self):
        mod = _reload({**FULL_ENV, "BUNNY_EGRESS_REGION": "sg"})
        self.assertEqual(
            mod.BUNNY_EGRESS_STORAGE_HOST, "sg.storage.bunnycdn.com")

    def test_native_and_s3_hosts_are_different(self):
        """Purging uses the native API, egress writes over S3. Conflating them
        makes the delete 401 and the raw mp4 stay public."""
        mod = _reload({**FULL_ENV, "BUNNY_EGRESS_REGION": "sg"})
        self.assertNotEqual(
            mod.BUNNY_EGRESS_STORAGE_HOST, mod.BUNNY_EGRESS_S3_HOST)

    def test_s3_host_is_never_the_native_edge_storage_host(self):
        """config/bunny_storage.py's host would authenticate against the
        wrong API entirely, so no default may ever resolve to it."""
        mod = _reload(FULL_ENV)
        self.assertNotEqual(
            mod.BUNNY_EGRESS_S3_HOST, mod.BUNNY_STORAGE_HOSTNAME,
        )


class EgressZoneCollisionTest(SimpleTestCase):

    def test_egress_zone_may_not_equal_the_cms_storage_zone(self):
        env = {
            **FULL_ENV,
            "BUNNY_STORAGE_ZONE": "shiksha-cms",
            "BUNNY_STORAGE_API_KEY": "cms-password",
            "BUNNY_EGRESS_ZONE": "shiksha-cms",
        }
        with self.assertRaises(ImproperlyConfigured):
            _reload(env)

    def test_distinct_zones_are_accepted(self):
        env = {
            **FULL_ENV,
            "BUNNY_STORAGE_ZONE": "shiksha-cms",
            "BUNNY_STORAGE_API_KEY": "cms-password",
            "BUNNY_EGRESS_ZONE": "shiksha-class-egress",
        }
        mod = _reload(env)
        self.assertTrue(mod.LIVEKIT_EGRESS_ENABLED)
        self.assertNotEqual(mod.BUNNY_EGRESS_ZONE, mod.BUNNY_STORAGE_ZONE)

    def test_unset_egress_zone_does_not_trip_the_guard(self):
        """Both unset is the default everywhere egress isn't configured —
        "" == "" must not be read as a collision."""
        env = {**FULL_ENV, "BUNNY_STORAGE_ZONE": "", "BUNNY_EGRESS_ZONE": ""}
        with self.assertWarns(RuntimeWarning):
            mod = _reload(env)
        self.assertFalse(mod.LIVEKIT_EGRESS_ENABLED)


def tearDownModule():
    """Restore the module to this sandbox's real configuration.

    Every test above leaves config.settings_base re-executed under a patched
    environment. Django's own `settings` object was populated at startup and
    is unaffected, but leaving the module object holding fake credentials
    would mislead anything that imports it directly later in the same run.
    """
    importlib.reload(importlib.import_module(BASE))
