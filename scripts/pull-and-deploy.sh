#!/usr/bin/env bash
# Pull latest from GitHub and rebuild/restart the stack.
# Run from cron, systemd timer, or manually:
#   */15 * * * * /path/to/esg/scripts/pull-and-deploy.sh >> /var/log/esg-deploy.log 2>&1
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
