#!/usr/bin/env bash
#
# scripts/deploy.sh — deploy this checkout to whatever box it is sitting on.
#
# WHY THIS EXISTS
# ---------------
# The four frontends deploy through a GitHub Action (.github/workflows/deploy.yml,
# on a push to Dev). The BACKEND has no Action and had no script: every deploy
# was hand-typed SSH. That is how a broken storage backend went unnoticed for
# months — nothing in the deploy path ever exercised it, because there was no
# deploy path, only whatever the operator remembered to type.
#
# So this encodes the sequence, including the storage round trip that would
# have caught it (see content/management/commands/check_media_storage.py and
# config/bunny_storage.py's note about Bunny not supporting HEAD).
#
# USAGE
#   scripts/deploy.sh                 # fetch, ff-merge, migrate, check, restart
#   scripts/deploy.sh --dry-run       # show what WOULD happen; changes nothing
#   scripts/deploy.sh --no-restart    # everything except restarting services
#   scripts/deploy.sh --skip-storage  # skip the storage round trip
#
# Environment is auto-detected, because dev and prod genuinely differ:
#   prod  /app/shiksha-backend                      uvicorn   branch main
#   dev   /home/appuser/backend/shiksha-backend     gunicorn  branch dev
#
# EXIT CODE
#   Non-zero if anything failed. Note the service is still restarted even when
#   the storage check fails — a degraded CDN is not a reason to leave the site
#   down — but the script exits non-zero so the failure cannot pass silently.

set -uo pipefail

DRY_RUN=0; RESTART=1; STORAGE=1
for arg in "$@"; do
  case "$arg" in
    --dry-run)      DRY_RUN=1 ;;
    --no-restart)   RESTART=0 ;;
    --skip-storage) STORAGE=0 ;;
    -h|--help)      sed -n '3,30p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg (try --help)" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# venv/ on both servers; .venv/ in the local worktrees.
PY=""
for cand in "$REPO/venv/bin/python" "$REPO/.venv/bin/python"; do
  [ -x "$cand" ] && { PY="$cand"; break; }
done
[ -n "$PY" ] || { echo "FATAL: no venv found under $REPO" >&2; exit 1; }

# Whichever web service this box actually runs.
WEB=""
for svc in uvicorn gunicorn; do
  systemctl list-unit-files "$svc.service" >/dev/null 2>&1 \
    && systemctl cat "$svc" >/dev/null 2>&1 && { WEB="$svc"; break; }
done
SERVICES="${WEB:+$WEB }celery celery-beat"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
run()  { if [ "$DRY_RUN" = 1 ]; then echo "  [dry-run] $*"; else "$@"; fi; }

say "environment"
echo "  repo:     $REPO"
echo "  python:   $PY"
echo "  branch:   $BRANCH"
echo "  services: ${SERVICES:-<none detected>}"
[ "$DRY_RUN" = 1 ] && echo "  MODE:     DRY RUN — nothing will change"

# A dirty tree means someone edited files on the server. A pull would either
# fail or bury their work; refuse and let a human look.
say "working tree"
DIRTY="$(git status --porcelain | grep -v '^?? ' || true)"
if [ -n "$DIRTY" ]; then
  echo "  REFUSING: tracked files are modified on this box:"
  echo "$DIRTY" | sed 's/^/    /'
  echo "  Commit, stash or revert them first."
  exit 1
fi
echo "  clean"

say "fetch"
run git fetch origin --quiet
INCOMING="$(git log --oneline "HEAD..origin/$BRANCH" 2>/dev/null || true)"
echo "  at:  $(git log --oneline -1 | cut -c1-64)"
if [ -z "$INCOMING" ]; then
  echo "  incoming: nothing — already up to date"
else
  echo "  incoming:"; echo "$INCOMING" | sed 's/^/    /'
fi

# --ff-only, never `reset --hard`: a non-fast-forward means the remote history
# was rewritten or someone committed here, and both deserve a human.
say "update"
if [ -n "$INCOMING" ]; then
  run git merge --ff-only "origin/$BRANCH" || {
    echo "  FATAL: not a fast-forward. Someone committed on this box, or the"
    echo "         remote branch was rewritten. Resolve by hand."
    exit 1
  }
  echo "  now: $(git log --oneline -1 | cut -c1-64)"
fi

say "migrations"
"$PY" manage.py migrate --plan 2>&1 | sed 's/^/  /'
run "$PY" manage.py migrate --noinput || { echo "  FATAL: migrate failed"; exit 1; }

say "django checks"
"$PY" manage.py check 2>&1 | sed 's/^/  /' || { echo "  FATAL: checks failed"; exit 1; }

# ── The storage round trip. THIS is the step that was missing. ──────────────
# After migrate (so any storage-related migration has run) and before the
# restart (so the result is known before traffic returns).
STORAGE_FAILED=0
if [ "$STORAGE" = 1 ]; then
  say "media storage round trip"
  if [ "$DRY_RUN" = 1 ]; then
    echo "  [dry-run] $PY manage.py check_media_storage"
  elif ! "$PY" manage.py check_media_storage 2>&1 | sed 's/^/  /'; then
    STORAGE_FAILED=1
    echo "  ^^ STORAGE IS DEGRADED. Uploads and any code reading a file's size"
    echo "     or existence are affected. Deploy continues so the site stays"
    echo "     up, but this script will exit non-zero."
  fi
else
  say "media storage round trip — SKIPPED (--skip-storage)"
fi

if [ "$RESTART" = 1 ] && [ -n "$SERVICES" ]; then
  say "restart"
  # shellcheck disable=SC2086
  run systemctl restart $SERVICES
  [ "$DRY_RUN" = 1 ] || sleep 6
  for s in $SERVICES; do
    state="$([ "$DRY_RUN" = 1 ] && echo "dry-run" || systemctl is-active "$s")"
    printf '  %-14s %s\n' "$s" "$state"
    [ "$DRY_RUN" = 1 ] || [ "$state" = "active" ] || STORAGE_FAILED=1
  done
else
  say "restart — SKIPPED"
fi

say "done"
if [ "$STORAGE_FAILED" != 0 ]; then
  echo "  completed WITH FAILURES (see above)"
  exit 1
fi
echo "  ok"
