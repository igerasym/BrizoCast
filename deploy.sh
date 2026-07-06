#!/usr/bin/env bash
# BrizoCast — Zero-downtime deploy on Raspberry Pi.
#
# Usage:
#   ./deploy.sh          # Pull latest code, rebuild image, restart services
#   ./deploy.sh --check  # Only check if there are new commits (exit 0 = updates available)
#
# Designed to run via cron or systemd timer (e.g. weekly). Logs to stdout (journald picks it up).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BRANCH="${DEPLOY_BRANCH:-main}"
LOG_PREFIX="[brizocast-deploy]"

log() { echo "$LOG_PREFIX $(date -Iseconds) $*"; }

# --- Check for updates ----------------------------------------------------- #
git fetch origin "$BRANCH" --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    log "Already up to date ($LOCAL). Nothing to do."
    [ "${1:-}" = "--check" ] && exit 1  # exit 1 = no updates
    exit 0
fi

if [ "${1:-}" = "--check" ]; then
    log "Updates available: $LOCAL → $REMOTE"
    exit 0
fi

# --- Pull & rebuild -------------------------------------------------------- #
log "Pulling $BRANCH ($LOCAL → $REMOTE)..."
git pull origin "$BRANCH" --ff-only

log "Rebuilding Docker image..."
docker compose build --quiet

# --- Restart with zero-downtime (sequential) ------------------------------- #
log "Restarting services..."
docker compose up -d --remove-orphans

log "Pruning old images..."
docker image prune -f --filter "until=168h" >/dev/null 2>&1 || true

log "Deploy complete. New HEAD: $(git rev-parse --short HEAD)"
