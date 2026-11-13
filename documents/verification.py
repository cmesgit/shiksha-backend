"""Every rule of community verification, in one place.

No view re-derives any of this. That is the lesson `quizzes/visibility.py`
records from the batch-scoping work: the moment two call sites each decide who
may see or do something, they drift, and the drift shows up as "it works on one
screen and not the other" months later.

The rules, verbatim from the design's "How it works" tab:

  * Only confirmed specialists' reviews count, and only in their own subjects.
  * Nobody can review their own upload. Each member reviews a version once.
  * A set with changes requested stays in Browse, marked Unverified, with its
    confirmations paused.
  * Uploading a new version clears earlier confirmations and restarts the count.
  * Two specialists requesting changes on the same version hides the set until
    a moderator has looked at it.
  * Editing the files of a Verified set returns it to Unverified.
"""
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from .fields import field_for_subject
from .models import (
    Document,
    DocumentTopic,
    InviteState,
    ReviewOutcome,
    ReviewerSelection,
    SpecializationSource,
    SpecializationStatus,
    SubjectSpecialization,
    VerificationInvite,
    VerificationReview,
    VerificationSettings,
    VerificationStatus,
)


# ─────────────────────────────────────────────────────────────────────────────
# Reading state
# ─────────────────────────────────────────────────────────────────────────────

def subjects_of(document):
    """The subjects a set is tagged with. Several — that is the point.

    The design's sample set is tagged under both Civil Engineering and
    Sociology, and EITHER makes a specialist eligible. Anything that asks "is
    this person a specialist in *the* subject" has already got it wrong.
    """
    return list(
        DocumentTopic.objects.filter(document=document)
        .exclude(subject="")
        .values_list("subject", flat=True)
    )


def confirmed_subjects(user):
    """Subjects this member's reviews count in.

    PENDING does not count. A pending row means somebody typed a subject into
    a box; only CONFIRMED means a teacher record, enough verified uploads, or
    an admin's decision stands behind it.
    """
    if not getattr(user, "is_authenticated", False):
        return []
    return list(
        SubjectSpecialization.objects.filter(
            user=user, status=SpecializationStatus.CONFIRMED,
        ).values_list("subject", flat=True)
    )


def _casefold(values):
    return {v.strip().casefold() for v in values if v and v.strip()}


def is_specialist(user, document):
    """Does this member hold a confirmed specialization in ANY of the set's
    subjects. Case-insensitive, because `subject` is free text everywhere."""
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(_casefold(confirmed_subjects(user)) & _casefold(subjects_of(document)))


def matching_subject(user, document):
    """Which subject the member is reviewing as — recorded on the review so
    the history still reads correctly if the specialization is later revoked."""
    mine = {s.strip().casefold(): s for s in confirmed_subjects(user)}
    for subject in subjects_of(document):
        hit = mine.get(subject.strip().casefold())
        if hit:
            return hit
    return ""


def has_reviewed(user, document):
    """On the CURRENT version. An earlier version's review does not block a
    member from looking at the corrected one — that is the whole point of
    asking for changes."""
    if not getattr(user, "is_authenticated", False):
        return False
    return VerificationReview.objects.filter(
        document=document,
        version=document.verification_version,
        reviewer=user,
    ).exists()


def can_review(user, document, settings=None):
    """(bool, reason). The reason is shown to the member, so it says what is
    true of them rather than "forbidden"."""
    settings = settings or VerificationSettings.load()

    if not getattr(user, "is_authenticated", False):
        return False, "Sign in to review."
    if document.is_removed:
        return False, "This set is no longer available."
    if document.owner_id == user.id:
        return False, "You cannot review your own upload."
    if document.verification_status == VerificationStatus.VERIFIED:
        return False, "This set is already verified."
    if document.verification_status == VerificationStatus.HIDDEN:
        return False, "This set is waiting for a moderator."
    if not is_specialist(user, document):
        return False, "You are not a confirmed specialist in this set's subjects."
    if has_reviewed(user, document):
        return False, "You have already reviewed this version."

    # Under "Admin assigns" the open queue does not exist — a specialist may
    # only review what was handed to them.
    if settings.reviewer_selection == ReviewerSelection.ADMIN_ASSIGNS:
        assigned = VerificationInvite.objects.filter(
            document=document,
            version=document.verification_version,
            reviewer=user,
            state=InviteState.OPEN,
        ).exists()
        if not assigned:
            return False, "An administrator assigns reviewers for this set."

    return True, ""


def confirmation_count(document):
    """Confirmations on the CURRENT version only.

    Reviews from earlier versions are kept — they are a record of what someone
    actually said — but they do not count, which is how "uploading a new
    version restarts the count" works without destroying history.
    """
    return VerificationReview.objects.filter(
        document=document,
        version=document.verification_version,
        outcome=ReviewOutcome.CONFIRMED,
    ).count()


def change_request_count(document):
    return VerificationReview.objects.filter(
        document=document,
        version=document.verification_version,
        outcome=ReviewOutcome.CHANGES_REQUESTED,
    ).count()


def progress(document, settings=None):
    """What the badge and progress text on a card need, in one query pair."""
    settings = settings or VerificationSettings.load()
    confirmations = confirmation_count(document)
    needed = settings.confirmations_needed
    return {
        "status": document.verification_status,
        "confirmations": confirmations,
        "needed": needed,
        "remaining": max(0, needed - confirmations),
        "version": document.verification_version,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Who gets asked
# ─────────────────────────────────────────────────────────────────────────────

def eligible_reviewers(document, settings=None, limit=None):
    """Confirmed specialists who could review this set, least busy first.

    Order and exclusions come straight from README §5a:
      * never the uploader;
      * never anyone at the uploader's institution, when that is on;
      * never someone already at their open-review limit;
      * fewest open reviews first;
      * reviewers whose own verified uploads are in the set's branch first,
        when "Prefer the same branch" is on;
      * widened to other subjects in the same field only if the set's own
        subjects turn up nobody.
    """
    settings = settings or VerificationSettings.load()
    subjects = subjects_of(document)
    if not subjects:
        return []

    def _query(subject_names):
        qs = SubjectSpecialization.objects.filter(
            status=SpecializationStatus.CONFIRMED,
            is_paused=False,
            subject__in=subject_names,
        ).exclude(user_id=document.owner_id).select_related("user")

        if settings.skip_uploader_institution and document.institution.strip():
            # "This also leaves out their colleagues and classmates." Matching
            # on the institution string an uploader typed is crude, but it is
            # the only institution signal Explore has, and a near-miss here
            # fails safe: it asks one person fewer, never one person more.
            qs = qs.exclude(
                user__documents__institution__iexact=document.institution.strip(),
            )

        qs = qs.annotate(
            open_reviews=Count(
                "user__verification_invites",
                filter=Q(user__verification_invites__state=InviteState.OPEN),
                distinct=True,
            ),
        ).filter(open_reviews__lt=settings.open_reviews_per_reviewer)

        return qs.order_by("open_reviews", "pk").distinct()

    # Case-insensitively widen the name list, since `subject` is free text on
    # both sides and an exact __in would miss "civil engineering".
    def _variants(names):
        out = set()
        for n in names:
            out.update({n, n.strip(), n.strip().title(), n.strip().lower()})
        return [v for v in out if v]

    found = list(_query(_variants(subjects)))

    if not found and settings.widen_to_same_field:
        fields = {field_for_subject(s) for s in subjects}
        siblings = set(
            SubjectSpecialization.objects.filter(
                status=SpecializationStatus.CONFIRMED,
            ).values_list("subject", flat=True)
        )
        widened = [s for s in siblings if field_for_subject(s) in fields]
        if widened:
            found = list(_query(_variants(widened)))

    if settings.prefer_same_branch:
        branches = _casefold(
            DocumentTopic.objects.filter(document=document)
            .exclude(branch="")
            .values_list("branch", flat=True)
        )
        if branches:
            def _branch_rank(spec):
                owns = DocumentTopic.objects.filter(
                    document__owner_id=spec.user_id,
                    document__verification_status=VerificationStatus.VERIFIED,
                ).values_list("branch", flat=True)
                return 0 if _casefold(owns) & branches else 1
            # Stable sort: the least-busy order from the query is preserved
            # within each rank, so "prefer the branch" breaks ties rather than
            # replacing the primary rule.
            found.sort(key=_branch_rank)

    return found[:limit] if limit else found


@transaction.atomic
def invite_reviewers(document, settings=None, actor=None):
    """Open invites for the current version under the automatic policy.

    Returns the invites created. Idempotent per (document, version, reviewer)
    — the unique constraint means a second call tops up rather than duplicating
    and a re-run after a partial failure is safe.
    """
    settings = settings or VerificationSettings.load()
    if settings.reviewer_selection != ReviewerSelection.AUTOMATIC:
        return []

    version = document.verification_version
    already = set(
        VerificationInvite.objects.filter(
            document=document, version=version,
        ).values_list("reviewer_id", flat=True)
    )
    open_now = VerificationInvite.objects.filter(
        document=document, version=version, state=InviteState.OPEN,
    ).count()
    wanted = settings.reviewers_invited_per_set - open_now
    if wanted <= 0:
        return []

    expires = timezone.now() + timezone.timedelta(days=settings.days_to_respond)
    created = []
    for spec in eligible_reviewers(document, settings):
        if spec.user_id in already:
            continue
        created.append(VerificationInvite.objects.create(
            document=document,
            version=version,
            reviewer_id=spec.user_id,
            subject=spec.subject,
            expires_at=expires,
        ))
        already.add(spec.user_id)
        if len(created) >= wanted:
            break
    return created


def needs_admin_attention(document, settings=None):
    """Whether a set should appear on the admin's "Needs attention" tab.

    The design is emphatic that this list stays short — "Most sets never
    appear here. This list only holds the ones the automatic flow could not
    finish." Three causes, and nothing else qualifies.
    """
    settings = settings or VerificationSettings.load()
    if document.verification_status == VerificationStatus.HIDDEN:
        return "hidden"
    if document.verification_status == VerificationStatus.VERIFIED:
        return ""
    if settings.notify_admins_when_no_reviewer and not eligible_reviewers(
        document, settings,
    ):
        return "no_reviewer"
    if settings.reviewer_selection == ReviewerSelection.AUTOMATIC:
        has_open = VerificationInvite.objects.filter(
            document=document,
            version=document.verification_version,
            state=InviteState.OPEN,
        ).exists()
        expired = VerificationInvite.objects.filter(
            document=document,
            version=document.verification_version,
            state=InviteState.EXPIRED,
        ).exists()
        if expired and not has_open:
            return "invites_expired"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Changing state
# ─────────────────────────────────────────────────────────────────────────────

def _apply_state(document, settings):
    """Recompute status from the reviews on the current version.

    Pure derivation — call it after any change to the reviews and it produces
    the same answer. Two reviewers confirming at the same instant therefore
    cannot produce a different result from the same two reviews arriving one
    after the other.
    """
    confirmations = confirmation_count(document)
    changes = change_request_count(document)

    if changes >= settings.change_requests_before_hidden:
        new_status = VerificationStatus.HIDDEN
    elif changes > 0:
        # "A set with changes requested stays in Browse, marked Unverified,
        # with its confirmations paused." Not a takedown — one reviewer must
        # not be able to remove anyone's upload from the library.
        new_status = VerificationStatus.CHANGES_REQUESTED
    elif confirmations >= settings.confirmations_needed:
        new_status = VerificationStatus.VERIFIED
    else:
        new_status = VerificationStatus.UNVERIFIED

    fields = []
    if document.verification_status != new_status:
        document.verification_status = new_status
        fields.append("verification_status")
        if new_status == VerificationStatus.VERIFIED:
            document.verified_at = timezone.now()
            fields.append("verified_at")
    if fields:
        document.save(update_fields=fields)
    return new_status


@transaction.atomic
def record_review(user, document, *, matches_tags, content_accurate,
                  files_complete, note="", settings=None):
    """Write one review and recompute the set's state.

    ⚠ `select_for_update` is load-bearing. Without it the last two reviewers of
    a set that needs three can both read two confirmations, both write a third,
    and both compute "still two short" — leaving a set with four confirmations
    sitting at Unverified forever, with nothing in the logs to explain it.
    """
    settings = settings or VerificationSettings.load()
    document = Document.objects.select_for_update().get(pk=document.pk)

    allowed, reason = can_review(user, document, settings)
    if not allowed:
        raise PermissionError(reason)

    all_yes = matches_tags and content_accurate and files_complete
    if not all_yes and not note.strip():
        raise ValueError("Say what is wrong and where, so the uploader can fix it.")

    review = VerificationReview.objects.create(
        document=document,
        version=document.verification_version,
        reviewer=user,
        subject=matching_subject(user, document),
        matches_tags=matches_tags,
        content_accurate=content_accurate,
        files_complete=files_complete,
        note=note.strip(),
        outcome=(ReviewOutcome.CONFIRMED if all_yes
                 else ReviewOutcome.CHANGES_REQUESTED),
    )

    VerificationInvite.objects.filter(
        document=document,
        version=document.verification_version,
        reviewer=user,
        state=InviteState.OPEN,
    ).update(state=InviteState.ANSWERED)

    status = _apply_state(document, settings)

    # Once a set is settled, the invites still out on it are not "missed" —
    # they stopped being answerable. SUPERSEDED rather than EXPIRED so the
    # inactivity counter does not punish a reviewer for being slower than two
    # other people.
    if status in (VerificationStatus.VERIFIED, VerificationStatus.HIDDEN):
        VerificationInvite.objects.filter(
            document=document,
            version=document.verification_version,
            state=InviteState.OPEN,
        ).update(state=InviteState.SUPERSEDED)

    maybe_earn_specialization(document.owner, settings)
    return review, status


@transaction.atomic
def bump_version(document, settings=None):
    """The files changed — restart verification.

    Covers both of the spec's wordings, which are the same event:
    "uploading a new version clears earlier confirmations and restarts the
    count", and "editing the files of a Verified set returns it to Unverified".

    Earlier reviews are NOT deleted. They stop counting because the count is
    per version, and the history panel on the document page is supposed to show
    that someone once asked for changes and the uploader fixed it.
    """
    settings = settings or VerificationSettings.load()
    document = Document.objects.select_for_update().get(pk=document.pk)

    VerificationInvite.objects.filter(
        document=document,
        version=document.verification_version,
        state=InviteState.OPEN,
    ).update(state=InviteState.SUPERSEDED)

    document.verification_version += 1
    document.verification_status = VerificationStatus.UNVERIFIED
    document.verified_at = None
    document.save(update_fields=[
        "verification_version", "verification_status", "verified_at",
    ])
    return document


@transaction.atomic
def expire_stale_invites(settings=None, now=None):
    """Pass an unanswered invite to the next reviewer.

    Returns (expired, paused). Lazily callable — nothing here depends on being
    run on a schedule, which matters because this codebase's Celery beat is not
    the only way this code runs.
    """
    settings = settings or VerificationSettings.load()
    now = now or timezone.now()

    stale = list(
        VerificationInvite.objects.select_related("document")
        .filter(state=InviteState.OPEN, expires_at__lt=now)
        # An admin's assignment is not rotated away from them by the sweep
        # that cycles automatic invites.
        .filter(assigned_by_admin=False)
    )
    if not stale:
        return [], []

    VerificationInvite.objects.filter(
        pk__in=[i.pk for i in stale],
    ).update(state=InviteState.EXPIRED)

    paused = []
    if settings.pause_inactive_reviewers:
        for invite in stale:
            specs = SubjectSpecialization.objects.filter(
                user_id=invite.reviewer_id, subject=invite.subject,
            )
            for spec in specs:
                spec.missed_invites += 1
                spec.is_paused = (
                    spec.missed_invites >= settings.missed_invites_before_pause
                )
                spec.save(update_fields=["missed_invites", "is_paused"])
                if spec.is_paused:
                    paused.append(spec)

    # Top the set back up, which is what "passes to the next reviewer" means.
    for document in {i.document for i in stale}:
        if document.verification_status in (
            VerificationStatus.UNVERIFIED, VerificationStatus.CHANGES_REQUESTED,
        ):
            invite_reviewers(document, settings)

    return stale, paused


# ─────────────────────────────────────────────────────────────────────────────
# Becoming a specialist
# ─────────────────────────────────────────────────────────────────────────────

def maybe_earn_specialization(user, settings=None):
    """A member becomes a reviewer in a subject through their own verified
    uploads — checked after any set reaches Verified.

    Creates CONFIRMED rows directly — this path has no admin step by design,
    and the admin switch that governs it is `members_can_earn_it`.
    """
    settings = settings or VerificationSettings.load()
    if not settings.members_can_earn_it or not getattr(user, "pk", None):
        return []

    counts = {}
    for subject in DocumentTopic.objects.filter(
        document__owner=user,
        document__is_removed=False,
        document__verification_status=VerificationStatus.VERIFIED,
    ).values_list("subject", flat=True):
        key = subject.strip()
        if key:
            counts[key] = counts.get(key, 0) + 1

    earned = []
    for subject, count in counts.items():
        if count < settings.verified_uploads_to_earn_it:
            continue
        spec, created = SubjectSpecialization.objects.get_or_create(
            user=user, subject=subject,
            defaults={
                "source": SpecializationSource.EARNED,
                "status": SpecializationStatus.CONFIRMED,
                "decided_at": timezone.now(),
            },
        )
        if created:
            earned.append(spec)
        elif spec.status == SpecializationStatus.PENDING:
            # They asked, and then earned it while waiting. Granting it is the
            # right answer — but NOT if an admin already said no. A declined
            # row stays declined; earning around a refusal would make the
            # admin's decision meaningless.
            spec.status = SpecializationStatus.CONFIRMED
            spec.source = SpecializationSource.EARNED
            spec.decided_at = timezone.now()
            spec.save(update_fields=["status", "source", "decided_at"])
            earned.append(spec)
    return earned


def sync_teacher_specializations(user, settings=None):
    """Teachers review in the subjects they teach, with no extra step.

    ⚠ Reads `TeachingAssignment`, not the TEACHER role, and filters
    `teacher__isnull=False`. Prod carries active PRIMARY assignment rows with
    nobody in them, and separately carries accounts holding the role with no
    assignments at all — keying on the role would confirm people who teach
    nothing, and skipping the null filter would try to confirm nobody.
    """
    settings = settings or VerificationSettings.load()
    if not settings.teachers_review_their_subjects:
        return []
    if not getattr(user, "pk", None):
        return []

    try:
        from courses.models import TeachingAssignment
    except Exception:  # pragma: no cover - courses is always installed
        return []

    names = set(
        TeachingAssignment.objects.filter(
            teacher=user, is_active=True, teacher__isnull=False,
        ).values_list("subject__name", flat=True)
    )

    created = []
    for name in sorted(n for n in names if n):
        spec, made = SubjectSpecialization.objects.get_or_create(
            user=user, subject=name,
            defaults={
                "source": SpecializationSource.TEACHER,
                "status": SpecializationStatus.CONFIRMED,
                "decided_at": timezone.now(),
            },
        )
        if made:
            created.append(spec)
    return created


# ─────────────────────────────────────────────────────────────────────────────
# Queues
# ─────────────────────────────────────────────────────────────────────────────

def review_queue(user, settings=None):
    """The sets waiting for this member, longest wait first.

    Under "Admin assigns" this is only what was handed to them. Under the other
    two policies it is every eligible set, because the design keeps the open
    queue alive even while invites are going out — an invite is a nudge, not a
    lock.
    """
    settings = settings or VerificationSettings.load()
    mine = confirmed_subjects(user)
    if not mine:
        return Document.objects.none()

    if settings.reviewer_selection == ReviewerSelection.ADMIN_ASSIGNS:
        assigned = VerificationInvite.objects.filter(
            reviewer=user, state=InviteState.OPEN,
        ).values_list("document_id", flat=True)
        qs = Document.objects.filter(pk__in=list(assigned))
    else:
        variants = set()
        for subject in mine:
            variants.update({subject, subject.strip(),
                             subject.strip().title(), subject.strip().lower()})
        qs = Document.objects.filter(
            topics__subject__in=[v for v in variants if v],
        )

    # ⚠ Exists(), not exclude(verification_reviews__reviewer=user).
    #
    # Two reasons, and both are silent failures. A plain exclude() across a
    # multi-valued relation drops a document if it has ANY matching review row,
    # so one review of version 1 would hide the corrected version 2 from the
    # same reviewer forever — the opposite of what asking for changes is for.
    # And the version comparison has to be against THIS document's current
    # version, which is an outer reference: only a correlated subquery can
    # express it.
    already_reviewed = VerificationReview.objects.filter(
        document=OuterRef("pk"),
        version=OuterRef("verification_version"),
        reviewer=user,
    )

    return (
        qs.filter(
            is_removed=False,
            verification_status__in=[
                VerificationStatus.UNVERIFIED,
                VerificationStatus.CHANGES_REQUESTED,
            ],
        )
        .exclude(owner=user)
        .annotate(_reviewed=Exists(already_reviewed))
        .filter(_reviewed=False)
        .distinct()
        .order_by("created_at")
    )
