# RC-010 — blocked: the shared source tree is not in version control

Date: 21 November 2026. **No code changed.** This is the blocker report
RC-010's prompt asks for: *"Stop dependent implementation if its frozen
interface is missing; continue independent inspection/tests and report the
exact blocker."*

## The blocker

RC-010's four "relevant existing files" are `shared/sync.mjs`,
`shared/README.md`, `shared/src/pages/GroupSessionLive.jsx` and
`shiksha-frontend/src/pages/GroupSessionLive.jsx`. Only the last exists.

All four web apps carry these scripts:

```json
"sync:shared":  "node ../shared/sync.mjs",
"check:shared": "node ../shared/sync.mjs --check"
```

`../shared/` is a **sibling of the app checkouts**, not a directory inside
any of them. In the spec author's workspace that was
`/home/hruaia/workspace/projects/work/shared/`. It is not in any
repository, and three independent checks agree:

| Check | Result |
|---|---|
| `sync.mjs` anywhere in the seven cloned repos | not found |
| `git ls-remote git@github.com:cmesgit/shared.git` | **Repository not found** |
| `find` for `sync.mjs` / sibling `shared/` on the dev VM | not found |

So the canonical shared tree exists **only on one workstation**, and
`npm run sync:shared` and `npm run check:shared` are **broken in all four
web apps** for anyone who clones from GitHub.

🔴 This is larger than RC-010. ProfileSwitcher, SettingsModal, apiClient,
ChatPanel, tokens.css and ~25 other files are duplicated across these apps
with a sync tool as their stated source of truth, and that source of truth
is unversioned and single-homed. A lost laptop is a lost canonical copy.

## Inventory of existing drift (RC-010 step 1, done independently)

Comparing SHA-256 of every file duplicated in two or more apps:

**26 files identical. 5 drifted.**

| File | Drift |
|---|---|
| `src/shared/ProfileSwitcher.jsx` | frontend ≠ (student = teacher) |
| `src/shared/SettingsModal.jsx` | frontend ≠ (student = teacher) |
| `src/shared/tokens.css` | **all three differ** |
| `src/shared/useNotificationNavigator.js` | student ≠ teacher |
| `src/pages/GroupSessionLive.jsx` | frontend (388 lines) ≠ (student = teacher, 361) |

The pattern is consistent: **`shiksha-frontend` is the outlier** in four of
the five. `tokens.css` is the worst case, with three distinct versions.

**The task says six drifts; I found five, and I cannot close that gap
honestly.** `sync.mjs` carries the manifest that defines which files are in
scope, and without it two things are invisible: a file I counted as
"identical" may not be synced at all, and a manifest entry **missing
entirely** from one app would not appear as drift in a comparison of files
that exist. `Admin-dashboard` is a concrete instance — it has both sync
scripts but **no `src/shared/` directory at all**, so it is registered for
sync and has received nothing.

## Why I did not write the two new files anyway

RC-010 asks for `shared/src/api/collaborationService.js` and
`shared/src/hooks/useCollaboration.js` — authored in the shared tree, then
synced into the apps.

Writing them directly into three app checkouts instead would create three
unsynced copies of a brand-new file: **precisely the drift this task exists
to eliminate**, and a direct violation of its own scope rule, "Do not edit
generated shared copies independently". Producing files that look like
progress while making the underlying problem worse is not progress.

## What unblocks this

One of:

1. **Recover `shared/` from the author's workstation and put it in version
   control** — a `cmesgit/shared` repository, cloned as a sibling. This is
   the smallest change and restores the tooling everywhere at once.
2. **Reconstruct it.** The 26 identical files are unambiguous canonical
   candidates. The 5 drifts each need a **contract-owner decision** on which
   version wins — that is RC-010's own instruction ("document canonical
   decision before sync"), and it is a product call, not an implementation
   one. `tokens.css` especially: three versions means three apps may render
   differently today.
3. **Abandon the sync model** and extract a published package. Much larger,
   and out of scope for RC-010.

Option 1 is strongly preferred and costs almost nothing if the directory
still exists.

## Status

RC-010 is **not started**, by design. Phase 1 remains complete. The next
task that can proceed without this is **RC-003's remaining hardware spike**
(Phase 0), which needs a Windows 11 machine and gates all of Phase 3.
