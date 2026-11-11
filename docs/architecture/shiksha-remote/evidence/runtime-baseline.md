# RC-004 evidence — runtime and deployment baseline

Run date: 11 November 2026, Asia/Kolkata. Static inspection of the repository plus locally installed package metadata. **No server was contacted and no deployment was performed.**

## The headline correction

The architecture pack carries "runtime incompatibility (Django 6 pin vs Python 3.11 Docker)" as a release gate, and RC-001 confirmed the mismatch exists. RC-004's job was to decide how much it matters. Answer: **less than it reads, for a reason worth recording.**

`scripts/deploy.sh` documents the real topology in its own header:

```
prod  /app/shiksha-backend                    uvicorn   branch main
dev   /home/appuser/backend/shiksha-backend   gunicorn  branch dev
```

and resolves its interpreter from `$REPO/venv/bin/python` or `$REPO/.venv/bin/python`. **Neither server runs the container.** `grep -rn docker scripts/*.sh` returns nothing. So `Dockerfile` and `docker-compose.yml` are unused build artifacts, not the production runtime, and the mismatch was never a live outage.

It was still a genuine defect — just a different one.

## What the mismatch actually was

| Fact | Evidence |
|---|---|
| Django 6.0.9 requires Python ≥3.12 | `Requires-Python: >=3.12` in the installed `django-6.0.9.dist-info/METADATA` |
| The image supplied Python 3.11 | `Dockerfile:1` was `FROM python:3.11-slim` |
| Nothing else needs above 3.10 | channels/daphne/celery ≥3.9; uvicorn/psycopg ≥3.10, all read from installed metadata |

Because `Requires-Python` is enforced by pip at resolution time, `RUN pip install --no-cache-dir -r requirements.txt` **fails the image build**. This was not a container that started and later misbehaved — it was a container that could not be built at all, and nobody noticed because nothing builds it.

Fix applied: `Dockerfile:1` → `FROM python:3.12-slim`, the minimum compatible version, with a comment recording both that reasoning and the fact that the image is not the deployment path. No dependency was upgraded.

## Compose path assumption

`docker-compose.yml` lives in `shiksha-backend/` but is written as though it sits one level up at the workspace root — `build: ./shiksha-backend`, and nginx mounts `./shiksha-frontend/dist`, `./student_dashboard/dist`, `./teacher_ui/dist`. From its own directory none of those paths resolve.

Per RC-004's "resolve compose path assumptions without moving repositories", this is recorded rather than fixed: moving the file changes a path that other tooling may assume, and relocating repositories is explicitly out of scope. Anyone running compose must invoke it from the workspace root with `-f shiksha-backend/docker-compose.yml`, and the sibling frontend build outputs must already exist.

## PostgreSQL target

| Source | Version |
|---|---|
| `docker-compose.yml` | `postgres:15` |
| Original architecture request | 16 |
| **Actually deployed (dev, measured 11 Nov 2026)** | **16.15** (Ubuntu 16.15-0ubuntu0.24.04.1) |
| **Actually deployed (prod, 11 Nov 2026)** | **16.15** (Ubuntu 16.15-0ubuntu0.24.04.1) |

**The compose pin is wrong, and the original request was right.** Dev runs PostgreSQL **16**, not the 15 that `docker-compose.yml` declares. Another symptom of the image being unbuilt and unmaintained: nothing reconciles it against a real environment. Anyone who did spin up that compose file for "local parity" would get a major behind.

Dev runtime measured over SSH, read-only:

```
/home/appuser/backend/shiksha-backend/venv/bin/python --version   → Python 3.12.3
… -c "import django; print(django.get_version())"                 → Django 6.0.9
sudo -u postgres psql -tAc "SHOW server_version;"                 → 16.15
```

So **dev already satisfies Django 6's `Requires-Python >=3.12` floor** — consistent with the finding that the 3.11 container is not what runs anywhere.

**Prod, reported by the operator** (not measured by the author — the same read-only commands were refused against the prod host by this environment's permission layer while succeeding against dev, so they were run by hand and the output pasted back):

```
sudo -u postgres psql -tAc 'SHOW server_version;'   → 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
/app/shiksha-backend/venv/bin/python --version      → Python 3.12.3
```

**Prod and dev match exactly on both.** Three conclusions, now evidenced rather than inferred:

1. **Nothing anywhere runs Python 3.11.** Both deployed environments are 3.12.3 and clear Django 6's floor. The container was the only thing claiming 3.11, and it is the only thing that does not run. The "runtime incompatibility" gate the pack carried from the start is **closed** — it was never a deployment risk, only an unbuildable artifact.
2. **`docker-compose.yml`'s `postgres:15` is wrong for both environments**, not just dev.
3. **PostgreSQL 16 is the migration target.** RC-005 can now plan its partial unique indexes and `SELECT … FOR UPDATE` concurrency against a known major instead of a guess — and must still be tested there, not on SQLite.

The dev/prod asymmetry recorded elsewhere in this repo (different paths, uvicorn vs gunicorn) holds for the service layer but **not** for the language and database majors, which are identical.

Django 6.0 supports PostgreSQL 14+, so 15 and 16 are both valid targets and nothing in this pack depends on choosing between them — with one exception RC-005 must respect: the partial unique indexes and `SELECT … FOR UPDATE` concurrency the consent state machine relies on behave identically on both, but must be tested on the **deployed** major, not SQLite. RC-001's suite is SQLite-based and explicitly does not prove this.

Both environments are now measured (above). The remaining acceptance criterion — a clean container build — is **waived by the architecture owner (11 Nov 2026): Docker is not part of this deployment and is not to be used.**

That is the right call and it resolves the ambiguity this task opened rather than leaving it. `Dockerfile` and `docker-compose.yml` are **not deployment artifacts**: nothing builds them, `scripts/deploy.sh` never references them, the image could not `pip install`, and the compose file pins a Postgres major that neither environment runs. They are dead configuration that reads as infrastructure.

The 3.12 bump stays — a dead file that is also wrong is worse than a dead file — but **do not treat either as describing how Shiksha runs**, and do not reconcile future runtime changes against them. The standing recommendation is deletion; it is left to the owner because removing deployment-shaped scaffolding deserves an explicit decision rather than a side effect of a runtime task.

## Migration and rollback posture

No migration was written, generated or applied by this task. For the phases that do add migrations (RC-005 onward), the posture the pack already requires:

- Staging only: `manage.py migrate --plan` reviewed before `manage.py migrate`, against the deployed Postgres major.
- Additive-only schema, so rollback is flag-off plus row revocation, never a down-migration — this is already ADR-01's position and `implementation-roadmap.md`'s Phase 1 rollback line.
- Rolling back this task specifically means reverting one line of `Dockerfile`. It touches nothing a server runs.

## Commands and results

```
grep -rn docker scripts/*.sh                 → no matches
which docker docker-compose                  → not installed on this machine
grep Requires-Python django-6.0.9 METADATA   → >=3.12
```

**Container build NOT run** — Docker is not installed on this machine, so "clean local/staging container build on supported Python" is **not satisfied**. The Python floor was established from package metadata, which is definitive for the pip-resolution failure but is not a substitute for a build. That acceptance criterion remains open and must be met by whoever has a Docker host, if the image is kept at all.

Existing regressions: RC-001's suite re-run after the `Dockerfile` edit — **9 tests, OK**. The edit cannot affect them (nothing imports the Dockerfile); recorded because the task requires running relevant tests once after changes.

## Recommendation beyond the patch

The image and compose file are unbuilt, unreferenced and were broken for an unknown period without anyone noticing. Either wire them into CI so they are exercised, or delete them — a build artifact nobody builds is a trap that reads as infrastructure. Flagged for the architecture owner; not actioned here, because deleting deployment scaffolding is outside a runtime-reconciliation task.

## Next

RC-003 (Windows capture/input feasibility) is the remaining Phase 0 task and the pack's load-bearing bet. It requires physical Windows hardware and is **blocked on this machine**. RC-002 depends on it and is therefore blocked too.
