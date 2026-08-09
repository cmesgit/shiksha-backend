"""skills/moderation.py — raising and resolving moderation flags.

Publishing a listing is instant by product decision (README.md, "Two decisions
worth revisiting"). The counterweight is that anything unusual lands in a queue
an admin can actually see — `ListingModerationFlag`. This module is the only
place that writes one, so the rate-limit rule lives in exactly one spot.
"""
import logging

from django.utils import timezone

from .listing_models import ListingModerationFlag

log = logging.getLogger(__name__)


def flag_for_review(expert, reason, listing=None, detail=""):
    """Raise a moderation flag, unless an identical one is already open.

    Deduplicated on (expert, reason, open) so a teacher publishing five
    listings in a row produces one queue item, not three.
    """
    existing = ListingModerationFlag.objects.filter(
        expert=expert, reason=reason, is_open=True
    ).first()
    if existing:
        return existing
    log.info("skills.moderation: flagging expert %s (%s)", expert.id, reason)
    return ListingModerationFlag.objects.create(
        expert=expert, listing=listing, reason=reason, detail=detail or "",
    )


def resolve_flag(flag):
    flag.is_open = False
    flag.resolved_at = timezone.now()
    flag.save(update_fields=["is_open", "resolved_at"])
    return flag
