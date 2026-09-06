"""
Personal insight figures for the public Quiz Hub's signed-in panels
(design_handoff_public_quiz_hub Phase 8).

── The rule this module exists to enforce ──────────────────────────────────
Every number here is computed from THIS account's own submitted attempts.
Nothing is projected, estimated, or benchmarked against other learners.

That matters because the design's fixture data carried claims none of which
survive contact with a real database:

    "+6% this month"        needs a month of history this account may not have
    "Top 12% of learners"   a percentile across a platform with 17 attempts
    "Based on your last 400 attempted questions"

The product decision for this page was *hidden, never faked*. Applied to a
guest that means not rendering the panels; applied to a signed-in learner
with two attempts it means returning two attempts' worth of truth and
letting the page say so. So `deltas` and percentile ranking are absent
rather than approximated, exactly as Phase 5 shipped no `attempt_count`
rather than a plausible one.

`accuracy` is over ANSWERED questions and `attempt_rate` is answered ÷
served. Keeping them apart is the point: a learner who leaves half the paper
blank and gets the rest right has high accuracy and low attempt rate, and
collapsing the two into "score" hides which of the two problems they have.
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import PracticeSet, PublicAttempt, PublicAttemptAnswer

# Used only when a subject tag carries no colour of its own. The hub needs a
# stable accent per subject for its chart columns and rings; falling back to
# one grey for everything would make the chart unreadable.
FALLBACK_COLORS = [
    "#0F9D6B", "#3b82f6", "#7C5CFC", "#FFB21D",
    "#12b3a6", "#ec4e86", "#E14D2A", "#0B5B3E",
]

# A question counts as ANSWERED unless it is blank. Blank is the pair
# (no choice, no recorded text) — the same test as the serializer's
# `was_blank`, and deliberately not just "choice is null": a choice the admin
# later edited away leaves the row with text but no choice, and that learner
# did answer. Conflating the two would tell them they skipped it.
ANSWERED = Q(selected_choice__isnull=False) | ~Q(selected_text="")


def _pct(part, whole):
    """Whole-number percentage, or None when there is nothing to divide by.

    None rather than 0: "no data yet" and "you scored zero" are different
    statements, and a ring showing a confident 0% for a learner who has not
    answered anything in that subject is a lie the caller cannot detect.
    """
    if not whole:
        return None
    return round(part * 100 / whole)


def _streak_days(dates, today):
    """Consecutive days ending today or yesterday, from distinct local dates.

    Yesterday still counts: a streak should not be reported as broken at
    00:01 by someone who practised at 23:00 and simply has not practised
    again yet. It breaks once a full day has been missed.
    """
    if not dates:
        return 0
    ordered = sorted(dates, reverse=True)
    if (today - ordered[0]).days > 1:
        return 0
    streak = 0
    cursor = ordered[0]
    for day in ordered:
        if day == cursor:
            streak += 1
            cursor -= timedelta(days=1)
        elif day < cursor:
            break
    return streak


def _subject_of(practice_set):
    tag = practice_set.subject_tag
    return {
        "label": tag.label,
        "slug": tag.slug,
        "color": tag.color or "",
    }


def build_summary(account, recent_limit=5, rec_limit=3):
    """Everything the four signed-in panels need, in one response."""
    attempts = list(
        PublicAttempt.objects
        .filter(account=account, submitted_at__isnull=False)
        .select_related("practice_set", "practice_set__subject_tag")
        .order_by("-submitted_at")
    )
    if not attempts:
        # The caller renders an invitation to start one, not zeroed rings.
        return {"has_attempts": False}

    answers = PublicAttemptAnswer.objects.filter(attempt__in=attempts)
    totals = answers.aggregate(
        served=Count("id"),
        correct=Count("id", filter=Q(is_correct=True)),
        answered=Count("id", filter=ANSWERED),
    )

    by_subject = _by_subject(answers)
    ranked = [s for s in by_subject if s["accuracy"] is not None]
    best = max(ranked, key=lambda s: s["accuracy"]) if ranked else None
    weak = min(ranked, key=lambda s: s["accuracy"]) if ranked else None

    local_dates = {
        timezone.localtime(a.submitted_at).date() for a in attempts
    }

    return {
        "has_attempts": True,
        "totals": {
            "attempts": len(attempts),
            "questions_served": totals["served"],
            "questions_answered": totals["answered"],
            "questions_correct": totals["correct"],
            # Of what they actually answered.
            "accuracy": _pct(totals["correct"], totals["answered"]),
            # How much of the paper they attempt at all.
            "attempt_rate": _pct(totals["answered"], totals["served"]),
            # Of the whole paper, blanks included — this is the score a
            # learner actually walks away with, so it is NOT the same as
            # accuracy and both are shown.
            "average_score": _pct(totals["correct"], totals["served"]),
            "streak_days": _streak_days(local_dates, timezone.localdate()),
        },
        "by_subject": by_subject,
        "best_subject": best,
        "weak_subject": weak,
        "recent": [_recent_row(a) for a in attempts[:recent_limit]],
        "recommendations": _recommendations(attempts, weak, rec_limit),
    }


def _by_subject(answers):
    """Accuracy per subject, for the chart. One query, not one per subject."""
    rows = (
        answers
        .values(
            "attempt__practice_set__subject_tag__label",
            "attempt__practice_set__subject_tag__slug",
            "attempt__practice_set__subject_tag__color",
        )
        .annotate(
            served=Count("id"),
            correct=Count("id", filter=Q(is_correct=True)),
            answered=Count("id", filter=ANSWERED),
        )
        .order_by("attempt__practice_set__subject_tag__label")
    )
    out = []
    for i, row in enumerate(rows):
        color = row["attempt__practice_set__subject_tag__color"]
        out.append({
            "label": row["attempt__practice_set__subject_tag__label"],
            "slug": row["attempt__practice_set__subject_tag__slug"],
            "color": color or FALLBACK_COLORS[i % len(FALLBACK_COLORS)],
            "served": row["served"],
            "answered": row["answered"],
            "correct": row["correct"],
            "accuracy": _pct(row["correct"], row["answered"]),
        })
    return out


def _recent_row(attempt):
    """One row of "Recently attempted".

    Carries the ATTEMPT id, so the page's Review button opens the real
    review of the real attempt. The fixture had to fabricate a plausible
    past attempt here because it had no such id.
    """
    spent = None
    if attempt.submitted_at and attempt.started_at:
        spent = max(0, int(
            (attempt.submitted_at - attempt.started_at).total_seconds()))
    subject = _subject_of(attempt.practice_set)
    return {
        "attempt_id": str(attempt.id),
        "set_slug": attempt.practice_set.slug,
        "set_title": attempt.practice_set.title,
        "subject": subject["label"],
        "subject_slug": subject["slug"],
        "color": subject["color"] or FALLBACK_COLORS[0],
        "score": attempt.score,
        "total": attempt.total,
        "submitted_at": attempt.submitted_at,
        "seconds_spent": spent,
    }


def _recommendations(attempts, weak, limit):
    """Up to `limit` published sets to try next, each with its reason.

    Only sets this account has NOT already attempted are offered — the panel
    is headed "What to practise next", and leading it with something already
    finished reads as the page not knowing what the learner has done.

    `available_count` is checked because a published set can still resolve to
    zero questions if its subject's curation was rolled back. Recommending
    one would send a learner into the start endpoint's 409.
    """
    attempted_ids = {a.practice_set_id for a in attempts}
    latest_subject_id = attempts[0].practice_set.subject_tag_id

    candidates = list(
        PracticeSet.objects
        .filter(status=PracticeSet.STATUS_PUBLISHED)
        .exclude(id__in=attempted_ids)
        .select_related("subject_tag", "subject_tag__cover_image")
        .order_by("display_order", "title")
    )
    candidates = [c for c in candidates if c.available_count > 0]

    picked, seen = [], set()

    def take(pool, why, note):
        for candidate in pool:
            if candidate.id in seen:
                continue
            seen.add(candidate.id)
            picked.append((candidate, why, note))
            return True
        return False

    if weak:
        take(
            [c for c in candidates if c.subject_tag.slug == weak["slug"]],
            "Weakest area",
            f"{weak['label']} is your lowest-scoring subject at "
            f"{weak['accuracy']}%. Practice here moves your overall accuracy "
            f"further than anywhere else.",
        )
    take(
        [c for c in candidates if c.subject_tag_id == latest_subject_id],
        "Same subject",
        f"You have just practised "
        f"{attempts[0].practice_set.subject_tag.label}. This is the next set "
        f"in it that you have not attempted.",
    )
    take(candidates, "New ground", "A subject you have not practised yet.")

    return [_rec_row(c, why, note) for c, why, note in picked[:limit]]


def _rec_row(practice_set, why, note):
    subject = _subject_of(practice_set)
    cover = practice_set.subject_tag.cover_image
    return {
        "slug": practice_set.slug,
        "title": practice_set.title,
        "subject": subject["label"],
        "subject_slug": subject["slug"],
        "color": subject["color"] or FALLBACK_COLORS[0],
        "icon": practice_set.subject_tag.icon or "",
        "cover_image": cover.file.url if cover and cover.file else None,
        "why": why,
        "note": note,
        "question_count": practice_set.available_count,
        "minutes": practice_set.minutes,
        "difficulty": practice_set.difficulty,
    }
