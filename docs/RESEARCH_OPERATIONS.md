# Research Hub operational handoff

2026-10-17. Applies to dev and production. Never approve reviewers merely
because they have a teacher role. Approval permits access to private papers
after an invitation is accepted.

## IMPORTANT — next-session checklist

Deferred by the owner on2026-10-17. Completed application changes and local
backup setup are deployed; the tasks below remain open.

- [ ] **Priority 1: private off-server Bunny backups.** Obtain a dedicated
  `shiksha-private-backups` storage zone with no connected Pull Zone/CDN, or
  a root-only configuration path containing account-management credentials
  to create it. Existing storage credentials access public CMS media only;
  account storage management returned401. Never put database bundles or
  unpublished papers in the public CMS zone.
- [ ] Configure automatic copies for dev and production with separate paths,
  private/encrypted storage, retention and a documented recovery-key location.
  Download a remote bundle, verify hashes and restore into a disposable
  directory before marking independent recovery complete.
- [ ] **Priority 2: choose and approve qualified reviewers.** Owner has not
  selected the teachers yet. Obtain names and subjects, check qualifications,
  and approve actual applications in Admin → Research Hub → Reviewer
  applications. Aim for two independent reviewers per subject plus spares.
  Do not invent applications or approve solely from a teacher role.
- [ ] **Optional: human inbox placement.** With an owner-supplied controlled
  inbox, send one authorized test and check Inbox/Spam. Worker/provider
  delivery already passed; no need to repeat that test or contact teachers
  and students as test recipients.

Keep research SMS **OFF**. This checklist is not a scheduled automation.
Daily local DB/manuscript bundles and extraction/hash checks already passed
on both environments. Off-server copying is not yet configured.

## Reviewer approvals

Both environments currently have zero reviewer applications. The owner has
not decided the first reviewers. No accounts were fabricated, enrolled or
approved. Candidates apply at `/explore/research` → Reviewer profile, listing
subjects, specialist topics and qualifications. An actual administrator
checks evidence, then approves under `/research` → Reviewer applications.
Obtain the owner's candidate list before approving anyone. Two independent
qualified reviewers per subject are needed; spare capacity is useful.

## Email verification

Production sender `Shiksha <noreply@shikshacom.com>`; Resend domain verified.
Read-only provider history showed three recent messages delivered. A single
operational message with no personal/manuscript data was queued through the
actual `notifications.deliver_email` worker to Resend's official labeled
test address. Worker result SUCCESS and provider event delivered confirmed.
Task `f49f3ea3-f0d3-4753-bc3b-488baa15be95`, provider message
`01a147f7-1871-7ce0-bec2-fc92117edb8b`.

This confirms the live worker/provider route, not placement in a human's
Inbox rather than Spam. No teacher/student was sent a test message. The
owner has not supplied a controlled inbox for that optional check. Research
SMS remains OFF. Respect opt-out email preferences on actual invitations.

## Local recovery bundles

Existing `shiksha-db-backup.timer` runs daily at01:30 UTC with up to5minutes
random delay. Its service takes the PostgreSQL dump first, then a new
ExecStartPost creates `/var/backups/shiksha-research/research-*.tar.gz`.
Each bundle includes the latest successful database dump (must be less than
one hour old), private manuscript files and a SHA-256 manifest. Bundle
contents are verified before atomic publication. Root-only directory0700,
archives0600,14-day retention. Existing database backup behavior is retained.

Installed source: `scripts/operations/research_backup.py` copied to
`/usr/local/bin/shiksha-research-backup.py`; drop-in template:
`scripts/operations/research-backup.conf.example`. Sources:

- Dev: `/home/appuser/backend/shiksha-backend/private_research`
- Production: `/app/shiksha-backend/private_research`

Check latest job: `systemctl status shiksha-db-backup.service` and
`journalctl -u shiksha-db-backup.service`. The timer can be enabled while
the last execution failed; inspect the service result and latest archive.

Verify/restore into a **new disposable directory**, never the live path:

```sh
python3 /usr/local/bin/shiksha-research-backup.py verify /var/backups/shiksha-research/research-TIMESTAMP.tar.gz --restore-to /root/research-recovery-TIMESTAMP
```

All hashes validate before extraction. Symlinks, special files, traversal,
duplicate entries and checksums that differ are rejected. Restore creates
private files0600/directories0700 and refuses to overwrite an existing target.
The SQL dump is included for operator-controlled recovery, not automatically
loaded into PostgreSQL. Do not restore against a running production database.
Private revisions are immutable, and no private-file deletion job currently
runs: taking the database snapshot before the manuscript copy permits extra
unreferenced newer files without losing database-referenced revisions.

Six isolated standard-library tests cover a PDF+DB round trip, empty source,
missing DB, symlinks, path escape and tampering. Each real server's new bundle
must also be verified and restored into a disposable root-only directory.
There are currently no real private manuscripts, so the live check validates
the DB bundle; the local fixture proves file recovery.

**Off-server recovery remains unconfigured.** These bundles share the
application server's disk. No independent backup destination or provider
snapshot coverage was available to verify. Owner must select private,
encrypted independent storage and a recovery-key location before claiming
protection from loss of the entire server. Never put private papers or these
database bundles in the public CDN/media storage.
