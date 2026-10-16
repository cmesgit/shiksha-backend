from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from content.models import HomeContentBlock, HomeFloater, PublishStatus
from content.permissions import IsStudioEditor


class HomeDisplayTests(TestCase):
    def setUp(self):
        cache.clear()
        cache.delete(IsStudioEditor.CACHE_KEY)
        user = get_user_model().objects.create_user(username="display-editor", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(user=user)
        self.block = HomeContentBlock.objects.create(section="hero", heading="Learn with ShikshaCom", status="published")
        self.url = f"/api/content/admin/home-content/{self.block.pk}/"

    def test_invalid_modes_are_rejected_by_direct_api(self):
        for value in ([], {"unknown": "hidden"}, {"body": "erase"}, {"heading": "hidden"}):
            with self.subTest(value=value):
                response = self.client.patch(self.url, {"display_settings": value}, format="json")
                self.assertEqual(response.status_code, 400, response.data)

    def test_partial_display_patch_preserves_other_choices(self):
        self.block.display_settings = {"subhead": "hidden", "floaters": "default"}
        self.block.save()
        response = self.client.patch(self.url, {"display_settings": {"floaters": "custom"}}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.block.refresh_from_db()
        self.assertEqual(self.block.display_settings, {"subhead": "hidden", "floaters": "custom"})

    def test_display_choices_are_public(self):
        self.block.display_settings = {"heading_secondary": "hidden", "floaters": "custom"}
        self.block.save()
        response = APIClient().get("/api/content/home-content/")
        self.assertEqual(response.status_code, 200)
        row = next(row for row in response.data if row["section"] == "hero")
        self.assertEqual(row["display_settings"], self.block.display_settings)

    def test_deleting_last_badge_does_not_restore_defaults(self):
        badge = HomeFloater.objects.create(section="hero", slot="cap", icon="cap", status="published")
        response = self.client.delete(f"/api/content/admin/home-floaters/{badge.pk}/")
        self.assertEqual(response.status_code, 204, response.data)
        self.block.refresh_from_db()
        self.assertEqual(self.block.display_settings["floaters"], "custom")
        self.assertFalse(HomeFloater.objects.filter(section="hero").exists())

    def test_draft_publish_preserves_live_collection_choice(self):
        response = self.client.put("/api/content/admin/pages/home/draft/", {"sections": {"hero": {"heading_secondary": "", "display_settings": {"heading_secondary": "hidden"}}}}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.client.patch(self.url, {"display_settings": {"floaters": "hidden"}}, format="json")
        response = self.client.post("/api/content/admin/pages/home/publish/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.block.refresh_from_db()
        self.assertEqual(self.block.heading_secondary, "")
        self.assertEqual(self.block.display_settings, {"floaters": "hidden", "heading_secondary": "hidden"})

    def test_invalid_display_mode_cannot_enter_a_draft(self):
        response = self.client.put("/api/content/admin/pages/home/draft/", {"sections": {"hero": {"display_settings": {"body": "invalid"}}}}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("hero", response.data["rejected"])
        self.assertNotIn("hero", response.data["draft"])

    def test_default_heading_and_hidden_button_can_publish_without_raw_values(self):
        response = self.client.put("/api/content/admin/pages/home/draft/", {"sections": {"hero": {"heading": "", "cta_secondary_label": "Retained words", "cta_secondary_href": "", "display_settings": {"heading": "default", "cta_secondary": "hidden"}}}}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.post("/api/content/admin/pages/home/publish/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def test_backfill_preserves_existing_blank_and_default_conventions(self):
        self.block.heading_secondary = ""
        self.block.save()
        about = HomeContentBlock.objects.create(section="about_hero", heading="Custom heading", heading_secondary="")
        migration = import_module("content.migrations.0042_explicit_home_display_choices")
        migration.preserve_existing_display(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
        self.block.refresh_from_db(); about.refresh_from_db()
        self.assertEqual(self.block.display_settings["heading_secondary"], "default")
        self.assertEqual(about.display_settings["heading_secondary"], "custom")
        self.assertEqual(self.block.display_settings["floaters"], "default")
