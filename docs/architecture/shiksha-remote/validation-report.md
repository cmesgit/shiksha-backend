# Architecture-stage validation report

Date:12 October2026, Asia/Kolkata. This report concerns documentation consistency and static repository inspection only. No native control implementation or OS support was tested.

## Performed

- Read the user-supplied master request and inspected backend, shared web modules, all three Live web surfaces and Flutter project. Recorded repository commits and verified concrete source paths in existing-system-audit.md.
- Read existing instant/group join policy, identity/token code, private/course/skill integration points, remote-control scaffolding, files/models/migrations, authenticated media delivery, Channels auth/consumers and deployment configuration.
- Checked official LiveKit/OS/browser/portal sources, including current COSMIC upstream merge status. External source URLs and research date are in sources.md; none establishes deployed support.
- Confirmed installed student livekit-client2.17.2 exposes sendFile, streamBytes and byte-stream handlers. Inspected writer/reader code before specifying cancellation/progress behavior; no runtime transfer test.
- Ran `node shared/sync.mjs --check`: **failed with six pre-existing drifts**, affecting AuthContext.jsx, ProfileSwitcher.jsx and SettingsModal.jsx in both dashboards. No sync/write was run and the drifts remain untouched.
- Ran `python3 shiksha-backend/docs/architecture/shiksha-remote/validate_spec.py`: checks local Markdown links, JSON examples, balanced fences,31 task IDs/dependency DAG and every task's claimed existing source file. Final result below. This script does not certify Mermaid rendering, OpenAPI completeness or security correctness.
- Checked backend worktree: only the new documentation directory is added; no tracked application code, migration or configuration diff. No production connection, deployment, messages to others or repository commits.

## Results

Final consistency check: **50 Markdown documents,31 tasks,0 consistency errors**. Task graph is acyclic. All15 requested deliverables A–O are linked from README in order. Each task includes dependencies, source ownership, API/event contracts, security invariants, steps, tests, acceptance, integration, done criteria and model capability guidance. RC-001 is the standalone first prompt.

## Not run / not claimed

Django tests, container build, PostgreSQL migration/concurrency tests, frontend builds, browser UI tests, LiveKit cloud connection tests, Windows/macOS/Linux device tests, installer measurements, signing/update verification, scanner tests and load tests were **not run** in this documentation-only stage. No production account/cloud prices or deployed versions were inspected. No independent architecture/security reviewer or coding sub-agent was launched.

## RC-001 executed — 11 November 2026

First task of the pack is done. `sessions_app/tests_collaboration_baseline.py` adds 8 tests pinning the instant-meeting join contract: two unrelated students meet with zero Enrollment rows and no Subject, two unrelated teachers likewise, direction is symmetric, and the denials hold (anonymous, un-admitted lobby arrival, three terminal states). Identity `<user_uuid>_<session_uuid>`, room `group_session_<session_uuid>` and the 12-key join payload are asserted against a real signed JWT decoded locally.

**A first draft of this suite failed 7 of 8 against the real `origin/main` and `origin/dev` tips.** The architecture pack was written against a branch 17 commits behind main, where an instant meeting's `admit_mode` defaulted to `"open"`; upstream it now defaults to `"lobby"` (`f572151`), so a non-host knocks and the host admits before any token is issued. The suite is corrected, states `admit_mode` explicitly, and pins the default in a dedicated test. The 12-key join payload is unchanged across all three bases. Detail in mismatch 0 of the evidence file. Current result: **9 OK on `origin/main`, 9 OK on `origin/dev`**.

Results against the original working branch: new suite **8 OK**; `tests_group_session_socket` + `tests_group_session_end` **26 OK**; `sessions_app livestream` **226 run, 4 failures + 1 error**, all five pre-existing missing-LiveKit-credential cases in `sessions_app.tests` and unchanged by this patch. `shared/sync.mjs --check` re-confirmed the same six drifts, read-only, no sync. No production code, migration or configuration was changed. Detail and discovered mismatches: [evidence/baseline-tests.md](evidence/baseline-tests.md).

One prerequisite below is now **confirmed rather than suspected**: `Dockerfile:1` is `python:3.11-slim` while `requirements.txt` pins `Django==6.0.9`, which needs Python ≥3.12.

Also patched in this pass, in the documents rather than in code: a coerced-consent threat (T22) added to the register, the open product/safeguarding decisions separated out of the ADRs (OD-01 student↔student control, OD-02 audit retention, OD-03 unknown recording status), and explicit kill criteria added to RC-003 so a failed feasibility finding escalates instead of being worked around. No independent architecture/security review has still been performed.

## RC-004 executed — 11 November 2026

Runtime baseline reconciled, and the pack's "Django 6 vs Python 3.11 Docker" gate is **downgraded, not closed**. `scripts/deploy.sh` documents prod running from a venv at `/app/shiksha-backend` under uvicorn and dev under gunicorn; `grep -rn docker scripts/*.sh` returns nothing. **The container is not the deployment path**, so the mismatch was never a live outage.

It was a real defect of a different kind: Django 6.0.9 declares `Requires-Python: >=3.12`, which pip enforces at resolution, so the 3.11 image could not complete `pip install` — it failed to **build**, and nobody noticed because nothing builds it. `Dockerfile:1` is now `python:3.12-slim`, the minimum compatible bump (nothing else pinned needs above 3.10). No dependency upgraded.

**Deployed runtimes are now measured, and the gate is closed.** Dev and prod both run **Python 3.12.3** and **PostgreSQL 16.15** (dev over SSH; prod run by the operator and pasted back, because this environment's permission layer refused the prod host). So nothing anywhere ran 3.11 — the container was the only thing claiming it and the only thing that does not run. `docker-compose.yml`'s `postgres:15` is wrong for both environments, and **PostgreSQL 16 is RC-005's migration target**. **Container build still not run** — Docker is not installed on this machine, so that one acceptance criterion stays open. `docker-compose.yml` additionally assumes it sits at the workspace root (`build: ./shiksha-backend`, sibling `dist/` mounts) and is recorded rather than moved. RC-001's suite re-run after the edit: **9 OK**. Detail: [evidence/runtime-baseline.md](evidence/runtime-baseline.md).

Phase 0 now stands at RC-001 and RC-004 done, RC-003 part-done (see below), and RC-002 blocked behind RC-003.

## RC-003 partially executed — 11 November 2026

Hardware-independent half done from published sources. **One Stop criterion is confirmed hit:** ADR-02's companion token specifies `can_subscribe: false` with `can_publish_data: true`, but `canSubscribe` governs data reception as well as media and LiveKit has no separate receive-data grant — the companion as designed would receive no input. The mitigation ADR-02 names (track-level subscription permissions) is **not confirmed to exist**. Separately, the Rust SDK does not reliably expose a data packet's sender, which the "controller is actual participant" invariant depends on; the upstream fix is an open, unmerged PR.

Consequence: **ADR-02 needs revision and RC-002 must not freeze schemas against it.** **Resolved 11 November 2026: Option C of [adr-02-revision-proposal.md](adr-02-revision-proposal.md) adopted** by the architecture owner, with Option B as the named fallback. The companion token now grants `can_subscribe: true`; media isolation becomes **client-side** (auto-subscribe off, no sink), which is recorded as an accepted trade rather than an equivalent; and authority moves from the SDK's sender field into the signed epoch, which removes the dependency on an unmerged upstream PR. New threat **T23** covers the risk this creates — a stolen device credential used by a non-companion client can subscribe to room media. RC-003's kill criteria rows 1–2 are rewritten so the spike tests the adopted design rather than the superseded one. **RC-002 is now unblocked on the ADR-02 axis.** The container-build criterion from RC-004 is **waived**: Docker is not part of this deployment. The remaining three findings (screen publish, coordinate fidelity, latency) are genuine measurements and still need the Windows spike — but hardware is no longer the first blocker. Detail: [evidence/sdk-capability-findings.md](evidence/sdk-capability-findings.md).

## Known prerequisites carried into tasks

Runtime incompatibility (Django6 pin vs Python3.11 Docker); actual deployed Postgres unknown vs compose15/request16; six shared-copy drifts; legacy UUID routing errors and incomplete consent/presence; Rust SDK screen/data grant feasibility; physical native-consent integrity and abrupt-kill cleanup; exact OS/compositor package certification; deployed recording policy and actual costs. These are explicit gates, not silently assumed fixes.
