"""
skills/marketing_models.py — admin-editable marketing copy for the SkillDev
marketplace (browse-page hero, "teach my craft" banner, Hub two-door copy).

One fixed row per `key` — the frontend falls back to its own hardcoded copy
when a block is missing or inactive, so an empty CMS never breaks the page.

Additive — no change to skills/models.py.
"""
import uuid
from django.db import models


class SkillMarketingBlock(models.Model):
    KEY_BROWSE_HERO = "browse_hero"
    KEY_TEACH_BANNER = "teach_banner"
    KEY_HUB = "hub"
    KEY_CHOICES = [
        (KEY_BROWSE_HERO, "Browse page hero"),
        (KEY_TEACH_BANNER, "Teach my craft banner"),
        (KEY_HUB, "Hub two-door landing"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=20, choices=KEY_CHOICES, unique=True)
    heading = models.CharField(max_length=200, blank=True)
    subheading = models.CharField(max_length=300, blank=True)
    body = models.TextField(blank=True)
    cta_label = models.CharField(max_length=60, blank=True)
    cta_url = models.CharField(max_length=300, blank=True)
    image = models.ImageField(upload_to="skills/marketing/", null=True, blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.get_key_display()
