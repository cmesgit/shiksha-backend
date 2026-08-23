"""Local-calendar helpers.

``TIME_ZONE`` is ``Asia/Kolkata`` and ``USE_TZ`` is on, so ``timezone.now()``
returns an aware datetime **in UTC**. Calling ``.replace(hour=0, ...)`` on that
value yields UTC midnight, which is 05:30 IST — not the start of the Indian
day. Every "today" / "this week" boundary in this codebase is a statement about
the Indian calendar (a class scheduled 09:00 IST, an assignment due "today"),
so it must be computed from the LOCAL date.

The bug this replaces was consistently in the same direction and easy to miss
in review, because the window is the right *length* and only the wrong
*offset*: anything between 00:00 and 05:29 IST fell outside "today" — a 05:00
class vanished from the hero, the calendar and the "Classes this week" count —
while yesterday's evening sessions reappeared as today's at 01:00 IST.

Several call sites in these same files already do ``timezone.localtime(now).date()``
correctly, some with comments warning about exactly this hazard, which is what
makes a single named helper worth more than four local fixes.
"""
from django.utils import timezone


def local_day_start(now=None):
    """Aware datetime at 00:00 **local** (IST) on the day ``now`` falls in.

    Pass ``now`` when the caller already has one, so every boundary in a single
    request is derived from the same instant rather than drifting across calls.
    """
    now = now or timezone.now()
    return timezone.localtime(now).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )


def local_today(now=None):
    """The local (IST) calendar date ``now`` falls on."""
    return timezone.localtime(now or timezone.now()).date()
