"""Give every existing expert exactly one SkillListing, then point every
existing session at it.

Before multi-skill, an ExpertProfile advertised one offering through its own
headline / hourly_rate / subject_description / intro video / mastery_target.
That offering becomes listing order=0, and every session ever booked was booked
against it — so the backfill is exact, not a guess.

Experts with no category are skipped: SkillListing.category is PROTECT/NOT NULL
and inventing one would put them in a category they never chose. They keep
working off the legacy ExpertProfile fields (which stay populated for exactly
this reason) and get a listing the first time they publish one.

Reversible: the reverse pass drops only the listings this migration created
(order=0 with no sessions attached beyond the ones it backfilled).
"""
from django.db import migrations


def seed(apps, schema_editor):
    ExpertProfile = apps.get_model("skills", "ExpertProfile")
    SkillListing = apps.get_model("skills", "SkillListing")
    SkillSession = apps.get_model("skills", "SkillSession")

    for expert in ExpertProfile.objects.select_related("category").iterator():
        if expert.listings.exists():
            continue
        category_id = expert.category_id
        if not category_id:
            # Fall back to the first entry of the M2M before giving up.
            category_id = expert.categories.values_list("id", flat=True).first()
        if not category_id:
            continue

        listing = SkillListing.objects.create(
            expert=expert,
            category_id=category_id,
            title=(expert.headline or "1-on-1 sessions")[:120],
            description=expert.subject_description or "",
            skill_tags=expert.skill_tags or [],
            price_paise=expert.hourly_rate or 0,
            intro_video_bunny_id=expert.intro_video_bunny_id or "",
            intro_video_status=expert.intro_video_status,
            intro_video_thumbnail_url=expert.intro_video_thumbnail_url or "",
            mastery_target=expert.mastery_target or 3,
            rating=expert.rating,
            sessions_count=expert.sessions_count or 0,
            is_active=True,
            order=0,
        )
        SkillSession.objects.filter(expert=expert, listing__isnull=True).update(
            listing=listing
        )


def unseed(apps, schema_editor):
    SkillListing = apps.get_model("skills", "SkillListing")
    SkillSession = apps.get_model("skills", "SkillSession")
    SkillSession.objects.filter(listing__order=0).update(listing=None)
    SkillListing.objects.filter(order=0).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("skills", "0026_skilllisting_listingmoderationflag_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
