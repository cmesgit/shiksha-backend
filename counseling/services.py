# PLACEMENT: backend/backend/counseling/services.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/counseling/services.py
#
# The rule-based pieces of the MVP spec, kept out of the views so the
# logic is testable and reusable:
#
#   match_counselors(learner_profile)  → ranked [(profile, score, reasons)]
#   bookable_slots(counselor, days)    → concrete datetimes from the weekly
#                                        availability minus booked sessions
#   booking_conflict(counselor, when, duration) → overlap check

from datetime import datetime, timedelta

from django.db.models import Prefetch
from django.utils import timezone

from .models import Appointment, AvailabilitySlot, CounselorProfile


# ── Matching ────────────────────────────────────────────────────────────
#
# Score = 3 × (specialization ∩ career_interests)
#       + 2 × stream-affinity hits
#       + 1 × shared language
# ties broken by rating, then experience of the platform (created_at).
# The spec's example — a Science student interested in Software
# Engineering → counselors in CS / Engineering Careers / Technology /
# University Admissions — falls out of interests (weight 3) + the
# stream map below (weight 2).

STREAM_AFFINITY = {
    "science": [
        "Computer Science & IT", "Engineering Careers", "Technology",
        "Medicine & Health Sciences", "University Admissions",
    ],
    "commerce": [
        "Commerce & Finance", "Business & Management", "Entrepreneurship",
        "University Admissions",
    ],
    "arts": [
        "Arts & Humanities", "Design & Creative Careers", "Media & Communication",
        "Civil Services & Government Exams", "University Admissions",
    ],
}

# Younger classes get stream/subject-selection guidance regardless of
# interests — Class 9-10 students usually haven't picked a stream yet.
EARLY_CLASS_SPECS = ["Stream Selection (Class 9–10)", "Career Discovery"]


def _bookable_qs():
    return (
        CounselorProfile.objects.filter(
            status=CounselorProfile.STATUS_APPROVED, is_listed=True
        )
        .prefetch_related("specializations", "availability")
    )


def match_counselors(learner_profile, limit=12):
    """Rank bookable counselors for one learner profile.

    Returns a list of dicts: {profile, score, reasons: [str]} sorted by
    score desc, rating desc. Counselors with score 0 still return (sorted
    last) so a brand-new platform with two counselors never shows an
    empty recommendation page.
    """
    intake = getattr(learner_profile, "counseling_intake", None)

    interest_ids = set()
    interest_names = {}
    languages = set()
    if intake is not None:
        for spec in intake.career_interests.all():
            interest_ids.add(spec.id)
            interest_names[spec.id] = spec.name
        languages = {x.lower() for x in intake.language_list()}

    stream = (getattr(learner_profile, "stream", "") or "").lower()
    stream_specs = set(STREAM_AFFINITY.get(stream, []))

    current_class = str(getattr(learner_profile, "current_class", "") or "")
    early = current_class in ("8", "9", "10")

    ranked = []
    for profile in _bookable_qs():
        score = 0
        reasons = []
        spec_names = {s.id: s.name for s in profile.specializations.all()}

        overlap = interest_ids & set(spec_names.keys())
        if overlap:
            score += 3 * len(overlap)
            reasons.append(
                "Matches your interests: "
                + ", ".join(sorted(interest_names[i] for i in overlap))
            )

        stream_hits = stream_specs & set(spec_names.values())
        if stream_hits:
            score += 2 * len(stream_hits)
            reasons.append(
                f"Guides {stream.title()}-stream students ("
                + ", ".join(sorted(stream_hits)) + ")"
            )

        if early:
            early_hits = set(EARLY_CLASS_SPECS) & set(spec_names.values())
            if early_hits:
                score += 2
                reasons.append("Helps Class 9–10 students choose a stream")

        if languages:
            lang_hits = languages & {x.lower() for x in profile.language_list()}
            if lang_hits:
                score += len(lang_hits)
                reasons.append(
                    "Speaks " + ", ".join(sorted(x.title() for x in lang_hits))
                )

        ranked.append({"profile": profile, "score": score, "reasons": reasons})

    ranked.sort(key=lambda r: (-r["score"], -float(r["profile"].avg_rating),
                               r["profile"].created_at.timestamp()))
    return ranked[:limit]


# ── Slots ───────────────────────────────────────────────────────────────

def bookable_slots(counselor, days=14, now=None):
    """Materialise the weekly availability into concrete datetimes for the
    next `days` days, stepping by the counselor's session duration, minus
    anything already booked (confirmed) and anything in the past.
    Returns a list of aware datetimes (server timezone), sorted."""
    now = now or timezone.now()
    step = timedelta(minutes=counselor.session_duration_minutes or 45)

    windows = list(counselor.availability.filter(is_active=True))
    if not windows:
        return []

    horizon_end = now + timedelta(days=days)
    booked = list(
        Appointment.objects.filter(
            counselor=counselor,
            status=Appointment.STATUS_CONFIRMED,
            scheduled_at__gte=now - timedelta(hours=6),
            scheduled_at__lte=horizon_end,
        ).values_list("scheduled_at", "duration_minutes")
    )

    def clashes(start, end):
        for b_start, b_dur in booked:
            b_end = b_start + timedelta(minutes=b_dur)
            if start < b_end and b_start < end:
                return True
        return False

    tz = timezone.get_current_timezone()
    slots = []
    for offset in range(days + 1):
        day = (now + timedelta(days=offset)).date()
        wd = day.weekday()
        for w in windows:
            if w.weekday != wd:
                continue
            cursor = timezone.make_aware(datetime.combine(day, w.start_time), tz)
            window_end = timezone.make_aware(datetime.combine(day, w.end_time), tz)
            while cursor + step <= window_end:
                if cursor > now and not clashes(cursor, cursor + step):
                    slots.append(cursor)
                cursor += step
    slots.sort()
    return slots


def booking_conflict(counselor, scheduled_at, duration_minutes):
    """True if a confirmed appointment overlaps [scheduled_at, +duration)."""
    end = scheduled_at + timedelta(minutes=duration_minutes)
    window = Appointment.objects.filter(
        counselor=counselor,
        status=Appointment.STATUS_CONFIRMED,
        scheduled_at__lt=end,
        scheduled_at__gt=scheduled_at - timedelta(hours=6),
    )
    for appt in window:
        if scheduled_at < appt.end_at and appt.scheduled_at < end:
            return True
    return False


def inside_availability(counselor, scheduled_at, duration_minutes):
    """True if the requested time fits inside one active weekly window."""
    end = scheduled_at + timedelta(minutes=duration_minutes)
    if end.date() != scheduled_at.date():
        return False
    local = timezone.localtime(scheduled_at)
    local_end = timezone.localtime(end)
    for w in counselor.availability.filter(is_active=True, weekday=local.weekday()):
        if w.start_time <= local.time() and local_end.time() <= w.end_time:
            return True
    return False
