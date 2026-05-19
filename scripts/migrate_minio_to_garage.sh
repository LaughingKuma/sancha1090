#!/usr/bin/env bash
# One-shot migration script. Copies all objects from MinIO to Garage
# using rclone running in a one-shot container on the opensky_net.
#
# Run this from the repo root, with .env present, while BOTH minio
# and garage services are running.
#
# Idempotent: rclone sync only copies changed/new objects. Safe to
# run multiple times — first pass for the warm copy, second pass
# after pausing DAGs for the delta.
#
# Removed from the repo in the cleanup commit at the end of the
# minio→garage migration PR.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "error: .env not found in repo root" >&2
  exit 1
fi

# Load .env into this shell so we can pass values to docker run -e.
set -a
# shellcheck disable=SC1091
source .env
set +a

: "${MINIO_ROOT_USER:?MINIO_ROOT_USER must be set in .env}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD must be set in .env}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY must be set in .env}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY must be set in .env}"

SRC_BUCKET="${MINIO_BUCKET:-opensky}"
DST_BUCKET="${S3_BUCKET:-opensky}"
NETWORK="${COMPOSE_PROJECT_NAME:-opensky}_net"

echo "Migrating: minio:${SRC_BUCKET} -> garage:${DST_BUCKET} on network ${NETWORK}"

docker run --rm \
  --network "${NETWORK}" \
  -e RCLONE_CONFIG_MINIO_TYPE=s3 \
  -e RCLONE_CONFIG_MINIO_PROVIDER=Minio \
  -e RCLONE_CONFIG_MINIO_ENDPOINT=http://minio:9000 \
  -e RCLONE_CONFIG_MINIO_ACCESS_KEY_ID="${MINIO_ROOT_USER}" \
  -e RCLONE_CONFIG_MINIO_SECRET_ACCESS_KEY="${MINIO_ROOT_PASSWORD}" \
  -e RCLONE_CONFIG_GARAGE_TYPE=s3 \
  -e RCLONE_CONFIG_GARAGE_PROVIDER=Other \
  -e RCLONE_CONFIG_GARAGE_ENDPOINT=http://garage:3900 \
  -e RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID="${S3_ACCESS_KEY}" \
  -e RCLONE_CONFIG_GARAGE_SECRET_ACCESS_KEY="${S3_SECRET_KEY}" \
  -e RCLONE_CONFIG_GARAGE_REGION=garage \
  rclone/rclone:1.66 \
  sync "minio:${SRC_BUCKET}" "garage:${DST_BUCKET}" \
  --progress --checksum --transfers 8

echo "Done. Verify with:"
echo "  docker run --rm --network ${NETWORK} \\"
echo "    -e RCLONE_CONFIG_GARAGE_TYPE=s3 -e RCLONE_CONFIG_GARAGE_PROVIDER=Other \\"
echo "    -e RCLONE_CONFIG_GARAGE_ENDPOINT=http://garage:3900 \\"
echo "    -e RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID=\"\${S3_ACCESS_KEY}\" \\"
echo "    -e RCLONE_CONFIG_GARAGE_SECRET_ACCESS_KEY=\"\${S3_SECRET_KEY}\" \\"
echo "    -e RCLONE_CONFIG_GARAGE_REGION=garage \\"
echo "    rclone/rclone:1.66 tree garage:${DST_BUCKET} | tail -30"
