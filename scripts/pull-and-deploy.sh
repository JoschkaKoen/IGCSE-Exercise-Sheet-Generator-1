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

# Require a clean tree except ignored files (optional safety)
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
  echo "$(date -Is) abort: working tree has local changes; commit or stash first."
  exit 1
fi

git fetch origin
BRANCH="${DEPLOY_BRANCH:-main}"
git checkout "$BRANCH"
OLD_HEAD="$(git rev-parse HEAD)"
git merge --ff-only "origin/$BRANCH"
NEW_HEAD="$(git rev-parse HEAD)"

if [[ "$OLD_HEAD" == "$NEW_HEAD" ]]; then
  echo "$(date -Is) no new commits on origin/$BRANCH"
  exit 0
fi

echo "$(date -Is) updated $OLD_HEAD -> $NEW_HEAD"
docker compose up -d --build
echo "$(date -Is) docker compose up -d --build done"

# Prune dangling images and all build cache so disk doesn't fill up over time.
docker image prune -f
docker builder prune -af
echo "$(date -Is) docker prune done"
