"""Connect community verification to the events that should drive it.

Two hooks, both of which were missing and both of which made a documented
rule silently untrue:

1. **A new set should go looking for reviewers.** Without this,
   `invite_reviewers()` was reachable only from inside the expiry sweep —
   which itself had no caller — so under the default "Invite automatically"
   policy nobody was ever invited to anything. The admin summary box promised
   "The 3 least busy reviewers in its subjects are invited" while no invite
   row had ever been created.

2. **A teacher should be confirmed in the subjects they teach.** Without this,
   `sync_teacher_specializations()` had no caller at all, so the admin
   setting "Teachers review in the subjects they teach, with no extra step"
   did nothing and a teacher had to request each subject like anyone else.

⚠ Both are wrapped so a failure cannot take down the write that triggered
them. Nobody should lose an upload because the matching query raised.
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


def _settings():
    """(global_enabled, VerificationSettings) — or (False, None) while off."""
    from global_settings.models import GlobalSettings
    if not GlobalSettings.load().explore_verification_enabled:
        return False, None
    from .models import VerificationSettings
    return True, VerificationSettings.load()


def invite_for(document):
    """Open invites for a set under the automatic policy, and notify.

    Called from the upload path and after a new version, rather than from a
    `post_save` on `Document`: the tag rows a match depends on are written
    *after* the parent, so a signal here would run against a set with no
    topics and find nobody.
    """
    enabled, settings = _settings()
    if not enabled:
        return []
    try:
        from . import verification as v
        from .verification_views import notify_invite

        invites = v.invite_reviewers(document, settings)
        for invite in invites:
            notify_invite(invite)
        return invites
    except Exception:
        logger.exception(
            "verification: could not invite reviewers for document %s",
            getattr(document, "pk", "?"),
        )
        return []


def _sync_teacher(user):
    enabled, settings = _settings()
    if not enabled or not settings.teachers_review_their_subjects:
        return
    try:
        from . import verification as v
        v.sync_teacher_specializations(user, settings)
    except Exception:
        logger.exception(
            "verification: could not sync teacher specializations for %s",
            getattr(user, "pk", "?"),
        )


def on_teaching_assignment_saved(sender, instance, **kwargs):
    """Confirm a teacher in a subject the moment they are staffed on it."""
    # ⚠ `teacher` is null=True / SET_NULL and prod carries ACTIVE PRIMARY rows
    # with nobody in them. Those are staffing gaps, not teachers.
    if not instance.is_active or instance.teacher_id is None:
        return
    _sync_teacher(instance.teacher)


def _connect_teaching_assignment():
    """Wire the teacher hook, if `courses` is installed.

    Guarded because `documents` must import cleanly in a configuration that
    does not ship the academy — the app is otherwise independent of it.

    ⚠ TWO things here are load-bearing, and the first one bit:
    
    1. `weak=False`. Django holds receivers by WEAK reference by default. The
       first version of this used `@receiver` on a function defined INSIDE
       this connector — so the moment the connector returned, the only strong
       reference went out of scope, the receiver was garbage-collected, and
       the signal silently had no listeners. Nothing errored; the teacher
       auto-confirm setting simply did nothing. The receiver is now a
       module-level function (a strong reference that lives as long as the
       module) AND connected with weak=False, so neither mistake can recur.
    2. `dispatch_uid`. `ready()` can run more than once under some test
       runners and management commands; without it the receiver connects
       twice and does its work twice.
    """
    try:
        from courses.models import TeachingAssignment
    except Exception:  # pragma: no cover - courses is always installed here
        return

    post_save.connect(
        on_teaching_assignment_saved,
        sender=TeachingAssignment,
        weak=False,
        dispatch_uid="documents_verification_teacher_sync",
    )


_connect_teaching_assignment()
