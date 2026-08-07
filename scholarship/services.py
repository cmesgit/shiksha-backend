"""
scholarship/services.py — business logic for the Instant Scholarship module.

Kept out of views.py so the rules that actually matter (dedup, server-owned
deadline, scoring) are testable without spinning up request objects, and so
tasks.py's Celery sweep and views.py's request handlers call the exact same
code paths rather than duplicating them.
"""
import hashlib
import random
from datetime import date

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    CheatSignalEvent,
    ExamAnswer,
    ExamQuestion,
    ExamSession,
    GuardianVerification,
    ScholarshipAward,
    ScholarshipBand,
    ScholarshipEligibilityRecord,
    ScholarshipQuestionBankItem,
    ScholarshipSettings,
)


class AlreadyAttemptedError(Exception):
    """Raised when the dedup constraint blocks a second attempt this year."""


class InsufficientQuestionBankError(Exception):
    """Raised when the bank can't fill a full paper for a class — surfaced
    loudly rather than silently shipping a short exam."""


class DeadlinePassedError(Exception):
    """Raised on any write attempt against a session past its deadline."""


# ── Identity / dedup ────────────────────────────────────────────────────

def normalize_child_name(full_name):
    return " ".join(full_name.strip().lower().split())


def compute_dedup_hash(guardian_verification, child_full_name, child_dob):
    """sha256(pepper | guardian's opaque verification reference | normalized
    child name | child DOB). Never hash the Aadhaar number itself, even
    alone — an unsalted hash of a 12-digit number is realistically
    reversible by brute force; this is peppered with a server secret and
    keyed off the reseller's own non-reversible token instead."""
    pepper = settings.SCHOLARSHIP_DEDUP_PEPPER
    payload = "|".join([
        pepper,
        guardian_verification.dedup_reference,
        normalize_child_name(child_full_name),
        child_dob.isoformat() if child_dob else "",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_or_reserve_eligibility(*, learner_profile, guardian_verification, academic_year):
    """Idempotent: a student re-checking eligibility before starting the
    exam (or reloading the instructions screen) must get the SAME reserved
    record back, not a false "already attempted" — that only happens once
    the record is actually consumed by a submitted exam. The
    UniqueConstraint on (dedup_hash, academic_year) among
    reserved/consumed rows is still the real enforcement; this just makes
    the read path resume-safe instead of create-or-fail."""
    dedup_hash = compute_dedup_hash(
        guardian_verification,
        learner_profile.full_name or f"{learner_profile.first_name} {learner_profile.last_name}",
        learner_profile.date_of_birth,
    )
    existing = (
        ScholarshipEligibilityRecord.objects
        .filter(dedup_hash=dedup_hash, academic_year=academic_year)
        .exclude(status=ScholarshipEligibilityRecord.STATUS_VOIDED)
        .order_by("-created_at")
        .first()
    )
    if existing is not None:
        if existing.status == ScholarshipEligibilityRecord.STATUS_CONSUMED:
            raise AlreadyAttemptedError(
                "One scholarship attempt is already recorded for this student this academic year."
            )
        return existing  # STATUS_RESERVED — resume path.

    try:
        with transaction.atomic():
            return ScholarshipEligibilityRecord.objects.create(
                dedup_hash=dedup_hash,
                academic_year=academic_year,
                learner_profile=learner_profile,
                guardian_verification=guardian_verification,
            )
    except IntegrityError:
        # Lost a race against a concurrent request for the same person.
        raise AlreadyAttemptedError(
            "One scholarship attempt is already recorded for this student this academic year."
        )


def start_or_resume_exam_session(eligibility_record, course, *, ip_address="", user_agent="", device_fingerprint=""):
    """One ExamSession per eligibility record (OneToOneField) — calling this
    twice for the same record returns the same in-progress session rather
    than minting a second timer, which is what makes 'reload the
    instructions page mid-flow' safe. `course` is only used on first
    creation; a resume ignores it (the course is fixed at first start)."""
    existing = getattr(eligibility_record, "exam_session", None)
    if existing is not None:
        return existing, False

    scholarship_settings = ScholarshipSettings.load()
    deadline = timezone.now() + timezone.timedelta(minutes=scholarship_settings.duration_minutes)
    session = ExamSession.objects.create(
        learner_profile=eligibility_record.learner_profile,
        course=course,
        eligibility_record=eligibility_record,
        deadline=deadline,
        ip_address=ip_address or None,
        user_agent=user_agent,
        device_fingerprint=device_fingerprint,
    )
    generate_exam_questions(session, scholarship_settings)
    return session, True


def expire_if_past_deadline(session):
    """Lazy expiry: any read/write against a session checks this first, so
    correctness never depends on the Celery sweep's polling interval — the
    sweep (tasks.expire_exam_sessions) is only a backstop for sessions
    nobody ever reloads after the deadline passes."""
    if session.status == ExamSession.STATUS_IN_PROGRESS and session.is_past_deadline:
        return submit_exam(session, auto_expired=True)
    return session


# ── Exam generation ─────────────────────────────────────────────────────

def _split_counts(total, pct_easy, pct_medium, pct_hard):
    """Turn percentages into integer counts that sum to exactly `total`,
    putting any rounding remainder on the easy bucket."""
    easy = round(total * pct_easy / 100)
    medium = round(total * pct_medium / 100)
    hard = total - easy - medium
    if hard < 0:
        # Degenerate config (e.g. easy=100, medium=50) — clamp rather than
        # ship a negative bucket count.
        medium = max(0, total - easy)
        hard = total - easy - medium
    return {
        ScholarshipQuestionBankItem.DIFFICULTY_EASY: easy,
        ScholarshipQuestionBankItem.DIFFICULTY_MEDIUM: medium,
        ScholarshipQuestionBankItem.DIFFICULTY_HARD: hard,
    }


def _distribute_across_subjects(count, subjects):
    """Spread `count` questions as evenly as possible across `subjects`,
    remainder going to the earlier subjects in the list."""
    n = len(subjects)
    base, remainder = divmod(count, n)
    return {
        subject: base + (1 if i < remainder else 0)
        for i, subject in enumerate(subjects)
    }


def generate_exam_questions(session, scholarship_settings=None):
    """Sample a per-student paper from the question bank and freeze it onto
    ExamQuestion rows with shuffled option order. Bulk-creates a matching
    blank ExamAnswer per question so autosave is always an UPDATE, never a
    get-or-create race."""
    scholarship_settings = scholarship_settings or ScholarshipSettings.load()
    class_level = session.course.class_level
    subjects = [choice[0] for choice in ScholarshipQuestionBankItem.SUBJECT_CHOICES]

    counts_by_difficulty = _split_counts(
        scholarship_settings.question_count,
        scholarship_settings.difficulty_easy_pct,
        scholarship_settings.difficulty_medium_pct,
        scholarship_settings.difficulty_hard_pct,
    )

    picked = []
    shortage = []
    for difficulty, needed in counts_by_difficulty.items():
        if needed <= 0:
            continue
        per_subject = _distribute_across_subjects(needed, subjects)
        for subject, n in per_subject.items():
            if n <= 0:
                continue
            pool = list(
                ScholarshipQuestionBankItem.objects.filter(
                    class_level=class_level, subject=subject, difficulty=difficulty, is_active=True,
                )
            )
            random.shuffle(pool)
            chosen = pool[:n]
            if len(chosen) < n:
                shortage.append(f"{subject}/{difficulty}: needed {n}, found {len(chosen)}")
            picked.extend(chosen)

    if len(picked) < scholarship_settings.question_count:
        raise InsufficientQuestionBankError(
            f"Class {class_level} question bank can't fill a full paper "
            f"({len(picked)}/{scholarship_settings.question_count}). Shortages: "
            + "; ".join(shortage)
        )

    random.shuffle(picked)
    picked = picked[: scholarship_settings.question_count]

    exam_questions = []
    for order, item in enumerate(picked):
        shuffled_options = list(enumerate(item.options))
        random.shuffle(shuffled_options)
        new_correct_index = next(
            i for i, (orig_index, _text) in enumerate(shuffled_options)
            if orig_index == item.correct_option_index
        )
        exam_questions.append(ExamQuestion(
            session=session,
            order=order,
            source_item=item,
            subject=item.subject,
            difficulty=item.difficulty,
            text=item.text,
            options=[text for _orig_index, text in shuffled_options],
            correct_option_index=new_correct_index,
        ))

    with transaction.atomic():
        created = ExamQuestion.objects.bulk_create(exam_questions)
        ExamAnswer.objects.bulk_create([ExamAnswer(question=q) for q in created])

    return created


# ── Answering ────────────────────────────────────────────────────────────

def record_answer(session, exam_question, *, selected_option_index, time_spent_seconds=0):
    if timezone.now() >= session.deadline:
        raise DeadlinePassedError("The exam deadline has passed.")
    if session.status != ExamSession.STATUS_IN_PROGRESS:
        raise DeadlinePassedError("This exam session is no longer in progress.")

    answer = exam_question.answer
    is_new_choice = answer.selected_option_index != selected_option_index
    answer.selected_option_index = selected_option_index
    answer.answered_at = timezone.now()
    answer.time_spent_seconds = time_spent_seconds
    if is_new_choice and answer.selected_option_index is not None:
        answer.change_count += 1
    answer.save()
    return answer


def clear_answer(session, exam_question):
    if timezone.now() >= session.deadline:
        raise DeadlinePassedError("The exam deadline has passed.")
    answer = exam_question.answer
    answer.selected_option_index = None
    answer.answered_at = None
    answer.save(update_fields=["selected_option_index", "answered_at"])
    return answer


# ── Anti-cheat signal logging ───────────────────────────────────────────

def log_cheat_signal(session, event_type, metadata=None):
    CheatSignalEvent.objects.create(session=session, event_type=event_type, metadata=metadata or {})
    if event_type == CheatSignalEvent.EVENT_TAB_HIDDEN:
        session.tab_switch_count += 1
        update_fields = ["tab_switch_count"]
        settings_obj = ScholarshipSettings.load()
        if (
            settings_obj.enable_tab_switch_tracking
            and session.tab_switch_count >= settings_obj.tab_switch_flag_threshold
            and not session.flagged_for_review
        ):
            session.flagged_for_review = True
            update_fields.append("flagged_for_review")
        session.save(update_fields=update_fields)
        return

    if event_type == CheatSignalEvent.EVENT_ANSWER_BURST:
        settings_obj = ScholarshipSettings.load()
        burst_count = CheatSignalEvent.objects.filter(
            session=session, event_type=CheatSignalEvent.EVENT_ANSWER_BURST
        ).count()
        if burst_count >= settings_obj.answer_burst_count_threshold and not session.flagged_for_review:
            session.flagged_for_review = True
            session.save(update_fields=["flagged_for_review"])


# ── Scoring / award ──────────────────────────────────────────────────────

def band_for_score(score):
    return (
        ScholarshipBand.objects
        .filter(is_active=True, min_correct__lte=score, max_correct__gte=score)
        .order_by("-min_correct")
        .first()
    )


def _parse_academic_year_start(value):
    """'2026-27' -> 2026. Falls back to the current year on anything
    unparseable rather than raising mid-scoring."""
    try:
        return int(str(value).split("-")[0])
    except (ValueError, IndexError):
        return timezone.now().year


def award_expiry_for(academic_year, scholarship_settings=None):
    scholarship_settings = scholarship_settings or ScholarshipSettings.load()
    start_year = _parse_academic_year_start(academic_year)
    expiry_date = date(
        start_year + 1, scholarship_settings.award_valid_until_month, scholarship_settings.award_valid_until_day,
    )
    return timezone.make_aware(
        timezone.datetime.combine(expiry_date, timezone.datetime.max.time())
    )


def submit_exam(session, *, auto_expired=False):
    """Score the exam and mint (or update) the award. Idempotent: calling
    this twice on an already-submitted session just returns the existing
    result rather than re-scoring or minting a second award — the server
    deadline sweep and a client-initiated submit can race harmlessly."""
    if session.status in (ExamSession.STATUS_SUBMITTED, ExamSession.STATUS_EXPIRED):
        return session

    from global_settings.models import GlobalSettings

    with transaction.atomic():
        answers = ExamAnswer.objects.select_related("question").filter(question__session=session)
        subject_breakdown = {}
        correct_count = 0
        for answer in answers:
            is_correct = answer.selected_option_index == answer.question.correct_option_index
            answer.is_correct = is_correct
            answer.save(update_fields=["is_correct"])
            if is_correct:
                correct_count += 1
            bucket = subject_breakdown.setdefault(answer.question.subject, {"correct": 0, "total": 0})
            bucket["total"] += 1
            if is_correct:
                bucket["correct"] += 1

        band = band_for_score(correct_count)
        discount_pct = band.discount_pct if band else 0

        session.score = correct_count
        session.awarded_discount_pct = discount_pct
        session.subject_breakdown = subject_breakdown
        session.status = ExamSession.STATUS_EXPIRED if auto_expired else ExamSession.STATUS_SUBMITTED
        session.submitted_at = timezone.now()
        session.save(update_fields=[
            "score", "awarded_discount_pct", "subject_breakdown", "status", "submitted_at",
        ])

        session.eligibility_record.status = ScholarshipEligibilityRecord.STATUS_CONSUMED
        session.eligibility_record.save(update_fields=["status"])

        if discount_pct > 0:
            free_mode = GlobalSettings.load().free_trial_enabled
            ScholarshipAward.objects.get_or_create(
                exam_session=session,
                defaults={
                    "learner_profile": session.learner_profile,
                    "course": session.course,
                    "discount_pct": discount_pct,
                    "academic_year": session.eligibility_record.academic_year,
                    "status": ScholarshipAward.STATUS_LOCKED if free_mode else ScholarshipAward.STATUS_ACTIVE,
                    "expires_at": award_expiry_for(session.eligibility_record.academic_year),
                },
            )

    return session


def get_active_award(learner_profile, course):
    """Used by enrollments' checkout integration to look up a redeemable
    award for this learner+course. Returns None if none exists or it has
    expired/been used/voided."""
    award = (
        ScholarshipAward.objects
        .filter(learner_profile=learner_profile, course=course)
        .exclude(status__in=[ScholarshipAward.STATUS_REDEEMED, ScholarshipAward.STATUS_VOIDED])
        .order_by("-created_at")
        .first()
    )
    if award and award.is_redeemable:
        return award
    return None


def redeem_award(award, *, enrollment=None, subscription=None):
    award.status = ScholarshipAward.STATUS_REDEEMED
    award.redeemed_at = timezone.now()
    award.redeemed_enrollment = enrollment
    award.redeemed_subscription = subscription
    award.save(update_fields=["status", "redeemed_at", "redeemed_enrollment", "redeemed_subscription"])
    return award
