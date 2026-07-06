# Account signals.
#
# The legacy one-User-one-Profile auto-create signal has been removed along
# with the Profile model. Learner identities now live on LearnerProfile and
# are created explicitly by the signup flow (signup_serializer) and lazily
# ensured at login via auth_flow._ensure_default_profile(). There is nothing
# to auto-create on User save anymore; this module is kept so the import in
# AccountsConfig.ready() stays valid and future signals have a home.
#
# M1 (Phase 3 architecture §6) — the first tenant of that "home": keep the
# Identity registry in sync with LearnerProfile / TeacherProfile. The
# migration (0022_populate_identity) backfills history via bulk_create,
# which Django deliberately does NOT run signals for — so there's no overlap
# or double-write between that one-time backfill and this ongoing sync.
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import LearnerProfile, TeacherProfile, Identity


def _teacher_display_name(tp):
    """Deliberately NOT imported from chat/views.py's near-identical helper:
    accounts owns the Identity registry and must not depend on chat (Phase 3
    seam rule 1 — only the owner writes its tables; the same discipline
    applies to which app may depend on which)."""
    try:
        lp = tp.user.default_learner_profile()
        if lp:
            name = f"{(lp.first_name or '').strip()} {(lp.last_name or '').strip()}".strip()
            if name:
                return name
            if getattr(lp, "full_name", ""):
                return lp.full_name
            if getattr(lp, "display_name", ""):
                return lp.display_name
        return tp.user.username or tp.user.email
    except Exception:
        return "Teacher"


@receiver(post_save, sender=LearnerProfile)
def sync_learner_identity(sender, instance, **kwargs):
    """Every LearnerProfile save upserts its Identity row. Covers both a
    brand-new profile (INSERT) and a display-name/is_active change on an
    existing one (UPDATE) with the same call — never raises, since a chat
    identity failing to refresh its cached name must not break profile
    editing."""
    try:
        Identity.objects.update_or_create(
            kind=Identity.KIND_LEARNER,
            profile_id=str(instance.id),
            defaults={
                "display_name": (instance.display_name or instance.full_name or "")[:150],
                "account_id": instance.account_id,
                "is_active": instance.is_active,
            },
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "accounts.signals: Identity sync failed for LearnerProfile %s", instance.id
        )


@receiver(post_save, sender=TeacherProfile)
def sync_teacher_identity(sender, instance, **kwargs):
    """Mirrors sync_learner_identity() for the teacher identity. TeacherProfile
    has no is_active flag (a teacher's per-track approval state is separate
    from whether their identity exists at all), so is_active is always True
    here — deactivating a teacher account is a User-level concern, not
    modeled on Identity in M1."""
    try:
        Identity.objects.update_or_create(
            kind=Identity.KIND_TEACHER,
            profile_id=str(instance.id),
            defaults={
                "display_name": _teacher_display_name(instance)[:150],
                "account_id": instance.user_id,
                "is_active": True,
            },
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "accounts.signals: Identity sync failed for TeacherProfile %s", instance.id
        )
