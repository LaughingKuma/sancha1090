#!/bin/bash
set -euo pipefail
SERVER_NAME="${SERVER_NAME:-opensky-collector}"

if ! hcloud server describe "$SERVER_NAME" >/dev/null 2>&1; then
    echo "server $SERVER_NAME does not exist; nothing to delete" >&2
    exit 0
fi

hcloud server delete "$SERVER_NAME"
echo "server $SERVER_NAME deleted. backfill any captured parquets via the backfill_from_buffer DAG."
