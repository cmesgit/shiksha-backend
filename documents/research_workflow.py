"""Research review orchestration. Call mutations while holding the paper row lock."""
import logging
from datetime import timedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q, Count
from django.utils import timezone
from .models import (ResearchSubmission as Paper, ResearchReviewer as Reviewer,
                     ResearchAssignment as Assignment, ResearchEvent as Event)

logger = logging.getLogger(__name__)


def remove_public_copy(storage, name):
    try:
        storage.delete(name)
    except Exception:
        logger.exception("Could not remove retired public research file %s", name)


LIVE_STATES = (Paper.Status.SUBMITTED, Paper.Status.INVITED, Paper.Status.REVIEWING)


def is_research_admin(user):
    return bool(user.is_authenticated and (user.is_superuser or user.has_role("ADMIN")))


def event(paper, actor, action, note=""):
    Event.objects.create(submission=paper, actor=actor, action=action, note=note, version=paper.current_version)


def alert(user, paper, title, note="", *, admin=False, reminder=False):
    from notifications.services import notify
    base = settings.RESEARCH_ADMIN_URL if admin else settings.RESEARCH_PUBLIC_URL
    url = f"{base.rstrip('/')}{'/research' if admin else '/explore/research'}?paper={paper.pk}"
    body = f"{note or 'Open Research Hub to see the submission status and next step.'}\n\n{url}"
    transaction.on_commit(lambda: notify(recipient=user,
        verb="research.reminder" if reminder else "research.updated", title=title,
        body=body, link_url=url, payload={"submission_id": paper.pk}))


def admin_alert(paper, title):
    for user in get_user_model().objects.filter(
        Q(is_superuser=True) | Q(user_roles__role__name="ADMIN", user_roles__is_active=True),
        is_active=True).distinct().iterator():
        if user.pk != paper.owner_id:
            alert(user, paper, title, admin=True)


def current_assignments(paper):
    return paper.assignments.filter(active=True, revision__version=paper.current_version)


def close_assignments(paper, status="closed"):
    current_assignments(paper).update(active=False, status=status)


def refresh_stage(paper):
    if paper.status not in (*LIVE_STATES, Paper.Status.APPROVAL):
        return
    rows = list(current_assignments(paper).values_list("status", flat=True))
    if rows.count("completed") >= paper.required_reviews:
        paper.status = Paper.Status.APPROVAL
    elif any(state in ("accepted", "completed") for state in rows):
        paper.status = Paper.Status.REVIEWING
    elif "invited" in rows:
        paper.status = Paper.Status.INVITED
    else:
        paper.status = Paper.Status.SUBMITTED
    paper.save(update_fields=["status", "updated_at"])


def invite(paper, reviewer, revision, *, actor=None, days=None):
    days = days or settings.RESEARCH_REVIEW_DAYS
    assignment = Assignment.objects.create(submission=paper, reviewer=reviewer.user, revision=revision,
        due_at=timezone.now()+timedelta(hours=settings.RESEARCH_INVITATION_HOURS), review_days=days)
    event(paper, actor, "auto_invite" if actor is None else "assign", "A qualified subject reviewer was invited. Waiting for acceptance.")
    alert(reviewer.user, paper, "You are invited to review a research paper",
          f"{paper.title}\nSubject: {paper.subject}. Accept or decline within {settings.RESEARCH_INVITATION_HOURS} hours. The manuscript stays private until acceptance.")
    return assignment


def fill_review_slots(paper):
    """Bounded batch, exact subject, preferred topic, soft workload cap, no repeat invitations."""
    if not paper.automatic_matching or paper.status not in LIVE_STATES:
        return 0
    current = current_assignments(paper)
    occupied = current.filter(status__in=["accepted", "completed"]).count()
    needed = paper.required_reviews - occupied
    if needed <= 0:
        current.filter(status="invited").update(active=False, status="filled")
        refresh_stage(paper)
        return 0
    pending = current.filter(status="invited").count()
    batch = min(needed + 1, 4) - pending
    if batch <= 0:
        return 0
    used = paper.assignments.filter(revision__version=paper.current_version).values_list("reviewer_id", flat=True)
    candidates = Reviewer.objects.filter(approved=True, available=True, user__is_active=True).exclude(
        user=paper.owner).exclude(user_id__in=used).select_related("user").annotate(
        workload=Count("user__research_assignments", filter=Q(user__research_assignments__active=True,
                        user__research_assignments__status__in=["invited", "accepted"])))
    ranked = [profile for profile in candidates if profile.workload < settings.RESEARCH_REVIEWER_CAPACITY
              and paper.subject.casefold() in {s.casefold() for s in profile.subjects}]
    ranked.sort(key=lambda profile: (
        -int(bool(paper.topic) and paper.topic.casefold() in {t.casefold() for t in profile.topics}),
        profile.workload, profile.pk))
    revision = paper.revisions.get(version=paper.current_version)
    for profile in ranked[:batch]:
        invite(paper, profile, revision)
    if ranked:
        paper.matching_notice_sent = False
    elif not pending and not paper.matching_notice_sent:
        paper.matching_notice_sent = True
        admin_alert(paper, "Research paper needs additional qualified reviewers")
        event(paper, None, "matching_wait", "No available new subject reviewer. Admin can assign or approve more reviewers.")
    paper.save(update_fields=["matching_notice_sent"])
    refresh_stage(paper)
    return min(len(ranked), batch)


def maintain_paper(paper, now=None):
    """Idempotent deadline sweep; caller holds select_for_update on this paper."""
    if paper.status not in LIVE_STATES:
        return {"expired": 0, "reminded": 0, "invited": 0}
    now = now or timezone.now()
    expired = reminded = 0
    for assignment in current_assignments(paper).filter(status__in=["invited", "accepted"]).select_related("reviewer"):
        if not assignment.reviewer.is_active or not Reviewer.objects.filter(user=assignment.reviewer, approved=True).exists():
            assignment.active, assignment.status = False, "revoked"
            assignment.save(update_fields=["active", "status"])
            expired += 1
            event(paper, None, "reviewer_revoked", "Reviewer is no longer eligible; the slot was released.")
        elif assignment.due_at <= now:
            assignment.active, assignment.status = False, "expired"
            assignment.save(update_fields=["active", "status"])
            expired += 1
            event(paper, None, "deadline_expired", "An invitation or review deadline expired; the slot was released.")
            alert(assignment.reviewer, paper, "Research review deadline expired", "Your access to this assignment has ended. A replacement review will be arranged.")
        elif assignment.reminded_at is None and assignment.due_at <= now+timedelta(hours=24):
            assignment.reminded_at = now
            assignment.save(update_fields=["reminded_at"])
            reminded += 1
            alert(assignment.reviewer, paper, "Research review deadline reminder",
                  f"{paper.title}\n{'Respond to the invitation' if assignment.status == 'invited' else 'Submit your review'} by {timezone.localtime(assignment.due_at).strftime('%d %b %Y, %I:%M %p %Z')}.", reminder=True)
    refresh_stage(paper)
    if expired and not paper.automatic_matching:
        admin_alert(paper, "Research review expired; manual reassignment needed")
    invited = fill_review_slots(paper)
    return {"expired": expired, "reminded": reminded, "invited": invited}
