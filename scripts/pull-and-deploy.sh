#!/usr/bin/env bash
# Fetch origin, fast-forward, then deploy the stack. Two modes:
#
#   PULL mode  (CI deploy) — IMAGE_TAG is set to the image's commit SHA. The image
#     was already built and pushed to GHCR by the Actions `build` job, so the VPS
#     only PULLS it (no apt/pip/gcc on the box). The host still fast-forwards to that
#     exact SHA because docker-compose bind-mounts ./output/eXam/bank and ./exams and
#     reads the compose files from this checkout — they must match the pulled image.
#
#   BUILD mode (manual / local / emergency) — IMAGE_TAG unset → build on the box with
#     `docker compose up -d --build`, the original behaviour (also the fallback if the
#     registry is unreachable).
#
# Triggers:
#   - GitHub Actions deploy job (PULL): IMAGE_TAG=<sha> ./scripts/pull-and-deploy.sh
#   - Manual (BUILD):                   ssh server && /root/esg/scripts/pull-and-deploy.sh
#
# ONE-TIME server setup for PULL mode: `docker login ghcr.io` with a PAT scoped
# read:packages (the GHCR package is private), persisted in /root/.docker/config.json.
#
# Do not poll GitHub on a timer; deploy when the repo changes (push → Actions).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROD=(-f docker-compose.yml -f docker-compose.prod.yml)

# Serialise concurrent deploys (manual ssh racing an Actions run, or two Actions
# runs sneaking past the workflow's concurrency cancel).
exec 9>"$REPO_ROOT/.deploy.lock"
if ! flock -n 9; then
  echo "$(date -Is) another deploy is in progress, exiting"
  exit 0
fi

# Require a clean tree except ignored files (optional safety).
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
  echo "$(date -Is) abort: working tree has local changes; commit or stash first."
  exit 1
fi

git fetch origin
BRANCH="${DEPLOY_BRANCH:-main}"
git checkout "$BRANCH"
OLD_HEAD="$(git rev-parse HEAD)"

# PULL mode pins the host to the image's exact commit so the bind-mounted files match
# the pulled image; BUILD mode follows the branch tip.
if [[ -n "${IMAGE_TAG:-}" ]]; then
  TARGET="$IMAGE_TAG"
else
  TARGET="origin/$BRANCH"
fi
echo "$(date -Is) OLD_HEAD=$OLD_HEAD target=$TARGET  (rollback: git reset --hard $OLD_HEAD && docker compose up -d --build)"
git merge --ff-only "$TARGET"
NEW_HEAD="$(git rev-parse HEAD)"

if [[ "$OLD_HEAD" == "$NEW_HEAD" && -z "${FORCE_DEPLOY:-}" ]]; then
  echo "$(date -Is) no new commits (HEAD already $NEW_HEAD); set FORCE_DEPLOY=1 to redeploy"
  exit 0
fi

echo "$(date -Is) updated $OLD_HEAD -> $NEW_HEAD"

# Pre-deploy disk guard. BUILD needs ~8 GB transient (TeX layer ~2 GB, pip ~1 GB,
# image ~6 GB, intermediate). PULL needs less, but the old and new images briefly
# coexist before the prune, so keep a 6 GB floor. Refuse rather than wedge mid-deploy.
if [[ -n "${IMAGE_TAG:-}" ]]; then GUARD=6; else GUARD=8; fi
avail_gb="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
if [ "${avail_gb:-0}" -lt "$GUARD" ]; then
  echo "$(date -Is) abort: only ${avail_gb:-0} GB free on /, need ${GUARD} GB"
  docker system df || true
  exit 1
fi

docker system df || true
if [[ -n "${IMAGE_TAG:-}" ]]; then
  echo "$(date -Is) PULL mode: ghcr image @ $NEW_HEAD (free=${avail_gb}G)"
  if docker compose "${PROD[@]}" pull && docker compose "${PROD[@]}" up -d; then
    echo "$(date -Is) pull + up -d done"
  else
    rc=$?
    echo "$(date -Is) PULL FAILED (rc=$rc); old container left running. Roll back: git reset --hard $OLD_HEAD && IMAGE_TAG=$OLD_HEAD docker compose ${PROD[*]} up -d"
    exit "$rc"
  fi
else
  echo "$(date -Is) BUILD mode: docker compose up -d --build (free=${avail_gb}G)"
  if docker compose up -d --build; then
    echo "$(date -Is) build + up -d done"
  else
    rc=$?
    echo "$(date -Is) build FAILED (rc=$rc); old container left running. Roll back: git reset --hard $OLD_HEAD && docker compose up -d --build"
    exit "$rc"
  fi
fi

# Bounded prune so cache/images don't fill the 40 GB disk over time. Time-based
# (keep anything used in the last 7 days) is safe; size-based would evict the ~2 GB
# TeX layer and force a cold rebuild. NOTE: `image prune -f` removes only *dangling*
# images — a stale *tagged* image (e.g. a renamed-away tag) needs an explicit rm.
docker image prune -f
docker builder prune -f --filter until=168h
echo "$(date -Is) docker prune done"
docker system df || true
