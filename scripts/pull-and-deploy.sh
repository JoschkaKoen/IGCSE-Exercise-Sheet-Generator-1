#!/usr/bin/env bash
# Fetch origin/main, fast-forward if there are new commits, then rebuild the stack.
# Docker is only run when the merge actually advances HEAD (no rebuild on no-op).
#
# Intended triggers:
#   - GitHub Actions on push to main (SSH runs this script).
#   - Manual: ssh server && /root/esg/scripts/pull-and-deploy.sh
#
# Do not use a cron job or systemd timer to poll GitHub; deploy when the repo changes
# (push → Actions), not on a fixed schedule.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Serialise concurrent deploys (manual ssh racing an Actions run, or two
# Actions runs sneaking past the workflow's concurrency cancel).
exec 9>"$REPO_ROOT/.deploy.lock"
if ! flock -n 9; then
  echo "$(date -Is) another deploy is in progress, exiting"
  exit 0
fi

# Require a clean tree except ignored files (optional safety)
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
  echo "$(date -Is) abort: working tree has local changes; commit or stash first."
  exit 1
fi

git fetch origin
BRANCH="${DEPLOY_BRANCH:-main}"
git checkout "$BRANCH"
OLD_HEAD="$(git rev-parse HEAD)"
echo "$(date -Is) OLD_HEAD=$OLD_HEAD  (manual rollback: git reset --hard $OLD_HEAD && docker compose up -d --build)"
git merge --ff-only "origin/$BRANCH"
NEW_HEAD="$(git rev-parse HEAD)"

if [[ "$OLD_HEAD" == "$NEW_HEAD" ]]; then
  echo "$(date -Is) no new commits on origin/$BRANCH"
  exit 0
fi

echo "$(date -Is) updated $OLD_HEAD -> $NEW_HEAD"

# Pre-build disk guard: a cold rebuild needs ~10 GB transient (TeX layer ~2 GB,
# pip layer ~1 GB, image ~5 GB, build context + intermediate ~2 GB). Refuse
# below 8 GB rather than failing mid-build and leaving a wedged state.
avail_gb="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
if [ "${avail_gb:-0}" -lt 8 ]; then
  echo "$(date -Is) abort: only ${avail_gb:-0} GB free on /, refusing to build"
  docker system df || true
  exit 1
fi

echo "$(date -Is) docker compose up -d --build starting (free=${avail_gb}G)"
docker system df || true
if docker compose up -d --build; then
  echo "$(date -Is) docker compose up -d --build done"
else
  rc=$?
  echo "$(date -Is) build FAILED (rc=$rc); old container left running. Roll back: git reset --hard $OLD_HEAD && docker compose up -d --build"
  exit "$rc"
fi

# Bounded prune so cache doesn't fill the 40 GB disk over time.
# Time-based --filter until=168h keeps any layer used in the last 7 days,
# regardless of total size. Size-based (--keep-storage / --max-used-space) is
# unsafe here: the TeX layer alone is ~2 GB and would be evicted when combined
# cache spikes, forcing a cold rebuild on the next deploy.
docker image prune -f
docker builder prune -f --filter until=168h
echo "$(date -Is) docker prune done"
docker system df || true
