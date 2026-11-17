# Integration evidence — plan branches brought onto the live deploy leaves

Date: **17 November 2026.** This is the §0 action from the implementation plan
(`shiksha-remote-opus-plan-review-2026-11-13.md`): the RC-001/002/004/005 work
had been sitting on two parallel branches since 11 November and was drifting
further from its deploy targets every day. Both are now merged up to today's
`origin/main` and `origin/dev` and re-verified.

## What was merged

| Branch | Was | Behind | Now |
|---|---|---|---|
| `docs/shiksha-remote-plan-on-main` | `c7a807b` | **86 commits** behind `origin/main` | `d0ee337` (merge commit) |
| `docs/shiksha-remote-plan-on-dev` | `76b6148` | **69 commits** behind `origin/dev` | `d9893e2` (merge commit) |

Both merges were taken by `ort` with **zero conflicts**, because **none of
those 155 commits touch `sessions_app/` or `livestream/`**:

    git log --oneline docs/shiksha-remote-plan-on-main..origin/main -- sessions_app/ livestream/
    # → 0 commits

That is the fact that made this cheap, and it is the fact that will stop being
true. The four days between the plan's "merge this week" and this merge cost
nothing; the next four might not.

## Migration state — the 0018/0019 collision did not recur

`origin/main` and `origin/dev` **both still top out at
`0018_instantcollaborationguest`** in `sessions_app`, so RC-005's `0019` lands
on the leaf it was generated against. After merging:

    manage.py migrate sessions_app --plan   # 0017 → 0018 → 0019, in order
    manage.py makemigrations --check --dry-run
    # → No changes detected

`makemigrations --check` is the load-bearing assertion here, not the plan
output: it proves the merged `models.py` and the migration graph still agree
after 155 commits of other people's work landed around them. A silent model
drift would have shown up here and nowhere else.

Note for whoever merges next: `global_settings` now carries **three different
`0018_*` migrations** plus two merge migrations (`0019_merge_20261115_1643`,
`0020_merge_20261115_2144`) from the admin-v2 / dashboards-v2 / explore work
landing in parallel. That is pre-existing and already resolved upstream — but
it is what the same mistake looks like when it is *not* caught, and it is why
the rule is never `makemigrations` from behind the leaf.

## Verification after the merge

Run on the merged `docs/shiksha-remote-plan-on-dev`, SQLite via
`config.settings_test`:

| Suite | Result |
|---|---|
| `sessions_app.tests_collaboration_baseline` (RC-001) + `sessions_app.tests_collaboration_models` (RC-005) | **35 tests OK** (9 + 26) |
| `sessions_app livestream` (full) | **264 tests, 4 failures + 1 error** |
| `contracts/test_contracts.py` (RC-002) standalone | **13 OK** |
| `contracts/test_openapi.py` (RC-002) standalone | **14 OK** |

The 5 non-passes are the documented environmental set — **the same five tests
by name**, all in `sessions_app.tests`, all needing real LiveKit credentials:

    ERROR: test_request_creates_participant   (RequestSessionTest)
    FAIL:  test_request_with_group_students   (RequestSessionTest)
    FAIL:  test_student_can_request_session   (RequestSessionTest)
    FAIL:  test_participant_can_view          (SessionDetailTest)
    FAIL:  test_start_session                 (SessionLifecycleTest)

**The total moved from 167 to 264 and the failure count did not.** That is the
assertion that matters and the reason this file lists the names rather than the
count: a future contributor reading only "4F+1E" cannot tell a preserved
baseline from a coincidence, and the plan's own guidance is to restate the
count in every evidence file. Restate the *names*.

The on-`main` branch was verified with `makemigrations --check` plus the same
35 RC tests (OK); the full suite was not re-run there because the two branches
carry byte-identical collaboration code and differ only in base.

## Cleanup done at the same time

The untracked byte-identical copy of `docs/architecture/shiksha-remote/` that
had been sitting in the `shiksha-backend` working tree since 11 November is
**deleted**. It was confirmed identical to the branch first (`diff -rq` against
`git archive` of the branch: only `__pycache__` differed), so nothing was lost.
It existed on an unrelated dirty branch and could only ever have become a
source of silent drift.

## Still open after this

RC-005's CASCADE/PROTECT divergence is **closed** (accepted 17 Nov 2026 — see
`collaboration-schema.md`). What remains before Track A can start is listed in
the workspace handoff `REMOTE.md`; nothing on this axis blocks RC-006.

The branches are **pushed, not merged into their targets, and no PR is open** —
a deliberate call by the owner on 17 Nov. They remain integration branches, so
this same merge-up has to be repeated before each next task. Do it from a
worktree (`~/workspace/projects/worktrees/remote-be`), never from the main
`shiksha-backend` checkout, which habitually carries another branch's
uncommitted work.
