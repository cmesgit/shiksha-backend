# Explore Research Hub v2

Updated2026-10-17. Workspace handoff: `../EXPLORE_PROGRESS.md` (outside this repository). V1 is deployed; check that file for v2 release status and exact revisions.

## Workflow and defaults

New papers: private PDF → automatic subject matching → bounded invitations → two independent reviewers → admin decision → publication. Admin can arrange reviews manually or change required count1–3 **before acceptance**. Existing v1 rows retain stored default1/manual settings. No existing publications are retroactively labeled as having two reviews.

- Subject match is case-insensitive exact approved expertise. Matching topic is preferred, then lower invitation/review workload; tie breaks by profile ID.
- Available approved active users only; exclude author and anyone already invited for this revision. No automatic approval from teacher role.
- A batch targets missing seats plus one spare (maximum4 invitations). The first required number to accept occupy the seats; remaining invitations close without file access. Paper row locks serialize acceptances.
- Workload soft limit3 invited/accepted assignments; manual assignment is an admin override. This is a matching preference/cap, not a global DB uniqueness promise across concurrent different papers.
- Invitation window48hours. Accepted review due7days after acceptance (admin manual assignment can choose1–30days).
- Beat runs every5minutes: one reminder24hours before each invitation/review deadline, expiry, access revocation, replacement matching. Exact expired deadlines also block acceptance/review/file APIs even if the next sweep has not run.
- No available reviewer: keep paper private, notify admins once per waiting episode, retry on sweeps. Approving a profile can make it eligible for the next sweep; admins can also use Find reviewers now.
- Reviewers can pause future invitations without losing existing accepted reviews. Expertise edits require fresh approval; accepted/completed active reviews must finish first.
- Each review needs relevance/clarity/methods/evidence/references checks plus explanatory feedback. Approval cannot contain unresolved checklist issues. Reviewers do not see other reviewers’ findings/identities while assessing a paper.
- Collect the stored required number of reviews. Objections prevent publication; admin can request changes, reject, or replace an assessment. A single-review legacy paper retains v1 immediate reviewer-changes behavior.
- Final publisher must be different from author and reviewers; every required current revision review must recommend approval and the reviewers must remain approved/active.

## Appeals

Only the author can appeal a rejected submission, once per version, with a reason. Rejection remains in effect during appeal. Admin upholds with explanation (fresh review round, not publication) or dismisses with explanation. Automatic matching excludes previously invited reviewers for that version; manual assignment cannot reuse original decision-makers on an upheld appeal. History preserves the original reviews, rejection, appeal and resolution.

## Corrections

Only the author of a visible published paper can propose a new corrected PDF and explanation. This creates a private numbered revision and open correction case, with fresh review requirements. The public Document keeps its ID, original file, metadata and published_version while the proposed correction is reviewed.

- Requesting changes creates further revision numbers and invalidates prior approvals.
- Rejecting/cancelling a proposed correction preserves the previous public version; the author can cancel only the correction, not withdraw the published original.
- Publishing a correction requires all current reviews, final admin confirmation, and a separate explicit **public correction notice** (private feedback is never automatically published).
- Publication updates the existing Document, preserves bookmarks/counts/links, records published_version and actual review count, retires the old public file, and exposes the correction notice on the public document page.
- A publication hidden by moderation cannot be silently restored by a correction.
- Withdrawal is an admin action and closes pending cases/assignments. Old downloaded copies cannot be recalled. Public copy deletion/purge is best effort through the existing storage backend; provider purge configuration still matters.

## Models / privacy

`ResearchSubmission`: current_version, required_reviews, automatic_matching, matching_notice_sent, published_version, published_review_count, optional public Document.
`ResearchRevision`: immutable numbered private PDF, digest, author response.
`ResearchReviewer`: user UUID, approved subject/topic expertise, availability, approved_by.
`ResearchAssignment`: exact revision/reviewer, active/status, due_at, review_days, accepted_at, reminded_at, decision/checks/feedback. DB constraint prevents duplicate active reviewer+revision assignments; parent locks enforce seat capacity.
`ResearchCase`: appeal/correction, version, reason, status, private resolution, separate public_note, admin/time. One open case per type; one case of each type per version.
`ResearchEvent`: actor/action/version/note/time retained for audit.

Administration requires superuser or active ADMIN role. Ordinary staff content editors and MODERATOR roles have no manuscript access. No self-review, self-reviewer-approval, self-publication or reviewer-as-final-publisher.

Private files use `RESEARCH_PRIVATE_ROOT` (default backend/private_research) outside MEDIA_ROOT/CDN. Include in persistent backups. Django check refuses roots inside public media. Only authorized API streaming with private/no-store headers; invited/unaccepted, expired, reassigned, suspended and closed reviewers cannot download. Only explicit published correction notices are public. No AI receives manuscripts or makes approval decisions.

## APIs under /api/explore/

- `GET/POST research/`: mine by default; `scope=reviews` current invitations/reviews; `scope=admin` admin queue. Pagination20; page/status/q and `case=appeal|correction` (open cases) filters.
- `GET/POST research/<id>/`: detail/actions. Writes require `action`, current integer `version`. Response includes own-compatible assignment, role-scoped assignments, review_progress, owner/admin cases/open_case/review_history.
- Actions: assign (reviewer_id UUID, optional assignment_id to replace, days), accept (expertise_confirmed/no_conflict/confidential), decline, review (decision/note/checks), changes, reject, publish (public_note required for corrections), withdraw.
- Additional actions: match; configure (automatic_matching, optional required_reviews1–3); appeal (note); uphold_appeal/dismiss_appeal (note); cancel_correction (note).
- `POST research/<id>/revisions/`: author revised PDF after changes requested, response, permission_confirmed=true.
- `POST research/<id>/corrections/`: author corrected PDF for a published paper, response/reason, permission_confirmed=true.
- `GET research/<id>/revisions/<revision_id>/file/`: authenticated permission-checked attachment.
- `GET/POST research/reviewers/`: own application/profile; `PATCH {available: bool}` changes availability only. Admin GET `?scope=admin`; admin PATCH `reviewers/<id>/ {approved: bool}` approves/revokes.
- PDF only, max50MB, extension/magic validation. Required title/abstract/authors/subject/rights declaration.

## Tasks / notifications / UI

`documents.tasks.sweep_research_reviews` is registered as `documents.sweep_research_reviews`; beat key `research-review-deadlines`, interval300seconds. Each paper locks independently; failures logged and do not stop the rest. Repeated sweeps do not repeat reminders/invitations. No broker needed for tests (invoke task inline).

`research.updated` and `research.reminder`: in-app, email opt-out, push opt-out, **SMS OFF**. Notifications dispatch after successful DB commit and contain environment-correct absolute links. Only bounded matched reviewers receive invitations; no mass notification to all teachers. Set RESEARCH_PUBLIC_URL and RESEARCH_ADMIN_URL for each environment.

Public `/explore/research`: submission, own papers, current independent reviews, reviewer availability, appeals and correction proposals/history. Public `/explore/doc/:id` shows actual published review count/version and explicit correction notices.
Admin `/research`: queue/search/status/open-case filters; reviewer approvals; review progress/all findings; matching/count/manual replacement controls; appeals and correction decisions/public notices. Both routes lazy loaded; no new dependency.

## Verification and deployment

Run `manage.py test documents notifications.tests_channel_isolation --settings=config.settings_test`, `manage.py check`, `makemigrations --check`, public `npm run build`, Admin `npx vite build`. Local tests use disposable SQLite/private storage and mocked outbound channels; SQLite does not prove PostgreSQL contention. Nullable Document joins on locked rows use `select_for_update(of=("self",))` for PostgreSQL.

Migrations0008(schema/constraints/defaults) and0009(published-version backfill) are additive to v1. New fields needed by old writers have DB defaults. Migrate before new API/UI. Then restart uvicorn, **celery worker and celery-beat**; verify registered task and schedule. Old generic workers must be restarted before beat begins sending the new task. Do not run fixtures or real-user notification tests on production.

Retain private files/tables on code rollback; do not reverse migrations after submissions. V1 code cannot safely administer papers with multiple active reviewers, so disable research writes/task scheduling during any rollback to v1 and retain current data for recovery.

## Still separate

Actual credential checks/approvals, real-provider email delivery verification, backup coverage and observed user usability. External plagiarism/journal integrations and expanded editorial workflows are not implemented. SMS remains deferred by the user's request.
