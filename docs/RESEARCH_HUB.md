# Explore Research Hub v1

Implemented 2026-10-17. Workspace-wide status and follow-up requests are tracked in `../EXPLORE_PROGRESS.md` (workspace root, outside this repository). This file travels with the backend code.

## Scope

Private PDF submission → administrator assigns an approved subject reviewer → reviewer accepts expertise/no-conflict/confidentiality declarations → structured review → author revision if needed → final administrator publication. One reviewer plus final admin decision. No automated broad invitations, reminders/reassignment, SMS, multiple reviewers, appeals or external journal integrations in v1.

Ordinary Explore documents still publish immediately. New uploads in the canonical `research-papers` category are redirected/refused in favor of this workflow. Existing research-category documents are preserved and have no new review badge. Labels entered by users do not confer reviewer credentials.

## Data and privacy

- `ResearchSubmission`: author, metadata, status, current_version, optional public Document.
- `ResearchRevision`: immutable numbered PDF, SHA-256, author response. Never use `.file.url`.
- `ResearchReviewer`: user (UUID), expertise statement, subjects/topics, explicit admin approval and availability.
- `ResearchAssignment`: invited/accepted/completed status, exact revision, deadline, decision, review checklist and feedback. Conditional DB uniqueness allows only one active assignment per paper.
- `ResearchEvent`: actor, action, version, note, time. Old revisions and decisions remain.
- Private storage is local `RESEARCH_PRIVATE_ROOT`, default `BASE_DIR/private_research`, **outside public MEDIA_ROOT and Bunny CDN**. Include it in backups and persistent volume provisioning. `manage.py check` refuses a root nested under MEDIA_ROOT.
- Files are streamed through authenticated APIs with private/no-store headers. Invitation access reveals metadata only; PDF/history require acceptance. Reassignment, suspension and closure remove reviewer access.
- Publication copies only the approved current revision into an ordinary public Document. The public serializer exposes review scope, authors and version; no private IDs, history or file paths.
- Withdrawal hides the public Document and deletes its public file via the storage backend (Bunny delete includes purge). Private revisions remain. Previously downloaded copies cannot be recalled. Verify CDN purge credentials operationally before promising immediate cache removal.
- PostgreSQL parent row locks serialize state transitions; a version number rejects stale revision actions; owner lock serializes duplicate submissions. SQLite tests verify transitions and uniqueness, not true PostgreSQL lock contention.

## Access

Research administration requires superuser or the active ADMIN role. Content-editor staff flags and generic moderator permissions do not grant manuscript access. Teachers/guest teachers/other specialists apply through the public Research Hub; admin checks credentials and approves. No automatic approval from role, no self-review or self-approval of reviewer credentials, no admin publication of their own submission.

## APIs (under /api/explore/)

- `GET/POST research/`: mine by default; `scope=reviews` assigned current reviews; `scope=admin` admin queue. List pagination20; page/status/q supported.
- `GET/POST research/<paper_id>/`: detail/actions. POST requires `action` and current integer `version`.
- Actions: assign (reviewer_id UUID, days1–30), accept (expertise_confirmed, no_conflict, confidential), decline, review (decision approve/changes/reject, note, checks), changes, reject, publish, withdraw.
- Review checks require relevance/clarity/methods/evidence/references with checked/issues/not_applicable values. Approval requires relevant subject checked and no unresolved issues.
- `POST research/<id>/revisions/`: revised PDF, permission_confirmed=true, response. Only author after changes requested. Restarts assignment; old review cannot authorize publication.
- `GET research/<id>/revisions/<revision_id>/file/`: permission checked authenticated attachment.
- `GET/POST research/reviewers/`: own profile/application; changing expertise resets approval. `GET ?scope=admin` lists applications for admin.
- `PATCH research/reviewers/<id>/`: admin `{approved: boolean}`. Revocation ends current assignments and returns pending papers to assignment queue.
- Upload is PDF only, max50MB, extension+magic validation. Manuscript file, title, abstract, authors, subject, permission declaration required.

## User interfaces

- Public app `/explore/research`, lazy loaded; tabs My submissions / Submit a paper / Review papers / Reviewer profile.
- Admin app `/research`, lazy loaded; queue/search/status filters, reviewer applications, assignment/reassignment, download, decisions, history.
- Published document page displays “Reviewed on Shiksha”, authors, version and scope. This is not a guarantee of scientific validity or a journal indexing claim.

## Notifications

`research.updated` uses existing policy-based notifications: in-app, email opt-out, push opt-out, SMS off. Dispatch occurs after successful DB commit. Assignments notify only assigned reviewer, not all teachers. Submission/revision/completed-review events notify superuser/ADMIN queue owners; decisions notify authors. Provider delivery was mocked during local tests; no real users were notified.

Set `RESEARCH_PUBLIC_URL` and `RESEARCH_ADMIN_URL` to environment-specific HTTPS origins. Defaults are production public/admin. Use dev URLs on the dev deployment.

## Verification / deployment

Run `python manage.py test documents notifications.tests_channel_isolation --settings=config.settings_test`, `manage.py check`, `makemigrations --check`. Public app `npm run build`; Admin `npx vite build` (local prebuild guard intentionally rejects localhost env). Migrations0006 and0007 are additive; migrate backend before deploying UIs. Keep old frontend assets while publishing new index last.

Local real-browser verification uses disposable `/tmp/shiksha-research-preview.sqlite3`, private/media paths under `/tmp/shiksha-research-preview-*`, with outbound email/push disabled in a temporary preview launcher. Never seed those fixtures on real dev/prod. Evidence in workspace `artifacts/shiksha-research-20261017/`.

Rollback: revert UI code first; revert backend code if necessary while leaving additive tables/private files intact. Do not reverse migrations or delete manuscripts after real submissions exist. Existing public Documents continue working independently.

## Remaining v2 work

Automatic topic/expertise matching and invitation batches; preference-controlled SMS with provider templates; automatic reminders/deadline reassignment; multiple independent reviews; appeals/corrections UX; plagiarism-provider integration; reviewer approval audit expansion and reviewer availability controls; observed usability with actual authors/reviewers/admins. No AI should approve papers or receive private manuscripts automatically.
