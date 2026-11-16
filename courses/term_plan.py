"""Term-plan pacing: is a subject ahead of, on, or behind its plan?

The student dashboard shows a status pill (AHEAD / ON TRACK / BEHIND) and a
tick on each progress bar marking "where the class should be by now". Both
need an EXPECTED position, which until now did not exist anywhere in this
codebase — there was no term plan, no planned date, nothing to compare actual
coverage against.

``Chapter.planned_date`` is that plan: the date a chapter is meant to be
covered. This module turns those dates into the expected fraction.

🔴 **Null planned dates are not "zero" — they mean NO PLAN.** A subject whose
chapters carry no dates returns ``None`` for both the status and the expected
fraction, and the client must then render progress WITHOUT a judgement. The
alternative — assuming an even pace across the year — produces confident
"BEHIND" labels that are an artefact of arithmetic rather than a fact about
the class, and "behind" on a student's own dashboard is not a guess worth
making.

🔴 A plan is only used when it covers ENOUGH of the subject. A syllabus with
2 of 30 chapters dated is a half-filled form, not a plan; judging a student
against it would compare their real progress to a fictional target. See
``MIN_PLANNED_RATIO``.
"""

from __future__ import annotations

from datetime import date

# A plan must cover at least this share of a subject's chapters before it is
# used for a judgement. Below it, the dates are treated as incomplete data.
MIN_PLANNED_RATIO = 0.6

# How far from the expected fraction still counts as "on track", as a share of
# the whole subject.
#
# 🔴 Must be strictly greater than 1/12 = 0.0833, so that being ONE chapter
# adrift in a twelve-chapter subject does not flip the pill to BEHIND. The
# first value here was 0.08, which failed its own stated intent by a
# thousandth — caught by the test, not by reading it. A single missed lesson
# should not change a student's status.
ON_TRACK_TOLERANCE = 0.09

STATUS_AHEAD = "ahead"
STATUS_ON_TRACK = "on_track"
STATUS_BEHIND = "behind"


def expected_fraction(planned_dates, today=None):
    """Share of the subject that SHOULD be covered by ``today``.

    ``planned_dates`` is one entry per chapter — ``None`` where that chapter
    has no planned date. Returns ``None`` when there is no usable plan, which
    callers must pass through rather than substituting 0.
    """
    total = len(planned_dates)
    if total == 0:
        return None

    dated = [d for d in planned_dates if d is not None]
    if len(dated) / total < MIN_PLANNED_RATIO:
        # A mostly-empty plan. Not enough to judge against.
        return None

    today = today or date.today()
    due = sum(1 for d in dated if d <= today)

    # Denominator is the WHOLE subject, not just the dated chapters: an
    # undated chapter is still part of the syllabus, and dividing by the
    # dated ones alone would inflate the expectation.
    return due / total


def pacing(chapters_done, chapters_total, planned_dates, today=None):
    """``(status, expected_fraction)`` for one subject.

    Both are ``None`` when there is no usable term plan — the signal the
    client needs to hide the pill and the tick instead of guessing.
    """
    if not chapters_total:
        return None, None

    expected = expected_fraction(planned_dates, today=today)
    if expected is None:
        return None, None

    actual = chapters_done / chapters_total
    delta = actual - expected

    if delta > ON_TRACK_TOLERANCE:
        return STATUS_AHEAD, expected
    if delta < -ON_TRACK_TOLERANCE:
        return STATUS_BEHIND, expected
    return STATUS_ON_TRACK, expected


def current_chapter(chapters):
    """The chapter the class is on: the first UNCOVERED one, by order.

    ``chapters`` is the already-built payload list (dicts with ``is_covered``
    and ``order``). Returns ``None`` once everything is covered — a finished
    subject has no "now", and showing the last chapter forever would read as
    if the class were stuck on it.
    """
    for ch in sorted(chapters, key=lambda c: (c.get("order") or 0)):
        if not ch.get("is_covered"):
            return ch
    return None
