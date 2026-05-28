#!/bin/bash
# Spin up the sancha1090-collector VPS on Hetzner with cloud-init.
# Auto-sources ../.env if R2_ENDPOINT isn't already set in the environment.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INIT_TMPL="$HERE/vps_init.sh"
COLLECTOR="$HERE/vps_collector.py"
ENV_FILE="$HERE/../.env"

if [ -z "${R2_ENDPOINT:-}" ] && [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-sancha1090}"
SERVER_NAME="${SERVER_NAME:-${PROJECT_NAME}-collector}"
SERVER_TYPE="${SERVER_TYPE:-cx23}"
IMAGE="${IMAGE:-debian-12}"
LOCATION="${LOCATION:-nbg1}"
SSH_KEY="${SSH_KEY:-}"

for var in OPENSKY_CLIENT_ID OPENSKY_CLIENT_SECRET R2_ENDPOINT R2_ACCESS_KEY R2_SECRET; do
    if [ -z "${!var:-}" ]; then
        echo "missing required env: $var" >&2
        exit 2
    fi
done

R2_BUCKET="${R2_BUCKET:-opensky-vps-buffer}"

if hcloud server describe "$SERVER_NAME" >/dev/null 2>&1; then
    echo "server $SERVER_NAME already exists" >&2
    hcloud server ip "$SERVER_NAME"
    exit 0
fi

ENV_FILE_CONTENTS=$(cat <<ENV_EOF
OPENSKY_CLIENT_ID=$OPENSKY_CLIENT_ID
OPENSKY_CLIENT_SECRET=$OPENSKY_CLIENT_SECRET
R2_ENDPOINT=$R2_ENDPOINT
R2_ACCESS_KEY=$R2_ACCESS_KEY
R2_SECRET=$R2_SECRET
R2_BUCKET=$R2_BUCKET
ENV_EOF
)
COLLECTOR_PAYLOAD=$(cat "$COLLECTOR")

USER_DATA=$(ENV_FILE_CONTENTS="$ENV_FILE_CONTENTS" COLLECTOR_PAYLOAD="$COLLECTOR_PAYLOAD" PROJECT_NAME="$PROJECT_NAME" \
    envsubst '${ENV_FILE_CONTENTS} ${COLLECTOR_PAYLOAD} ${PROJECT_NAME}' <"$INIT_TMPL")

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT
printf '%s\n' "$USER_DATA" >"$TMPFILE"

SSH_ARG=()
if [ -n "$SSH_KEY" ]; then
    SSH_ARG=(--ssh-key "$SSH_KEY")
fi

hcloud server create \
    --name "$SERVER_NAME" \
    --type "$SERVER_TYPE" \
    --image "$IMAGE" \
    --location "$LOCATION" \
    --user-data-from-file "$TMPFILE" \
    "${SSH_ARG[@]}"

echo
echo "server $SERVER_NAME created. cloud-init takes ~60s to install + first collection."
echo "watch progress: hcloud server ssh $SERVER_NAME -- journalctl -fu ${PROJECT_NAME}-collector"
