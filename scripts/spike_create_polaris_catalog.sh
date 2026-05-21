#!/bin/sh
set -eu

apk add --no-cache jq > /dev/null

REALM=POLARIS
CATALOG=opensky
BASE_LOCATION="s3://${S3_BUCKET}/warehouse/"
S3_ACCESS_KEY="${S3_ACCESS_KEY:?S3_ACCESS_KEY missing}"
S3_SECRET_KEY="${S3_SECRET_KEY:?S3_SECRET_KEY missing}"

TOKEN=$(curl -s -X POST http://polaris:8181/api/catalog/v1/oauth/tokens \
  -H "Polaris-Realm: ${REALM}" \
  -d "grant_type=client_credentials&client_id=${POLARIS_ROOT_CLIENT_ID}&client_secret=${POLARIS_ROOT_CLIENT_SECRET}&scope=PRINCIPAL_ROLE:ALL" \
  | jq -r .access_token)

PAYLOAD=$(cat <<JSON
{
  "catalog": {
    "name": "${CATALOG}",
    "type": "INTERNAL",
    "readOnly": false,
    "properties": {
      "default-base-location": "${BASE_LOCATION}"
    },
    "storageConfigInfo": {
      "storageType": "S3",
      "allowedLocations": ["${BASE_LOCATION}"],
      "region": "garage",
      "endpoint": "http://garage:3900",
      "endpointInternal": "http://garage:3900",
      "pathStyleAccess": true,
      "stsUnavailable": true
    }
  }
}
JSON
)

STATUS=$(curl -s -o /tmp/resp.json -w "%{http_code}" \
  -X POST http://polaris:8181/api/management/v1/catalogs \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Polaris-Realm: ${REALM}" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}")

if [ "${STATUS}" = "201" ] || [ "${STATUS}" = "409" ]; then
  echo "Catalog '${CATALOG}' OK (HTTP ${STATUS})"
  cat /tmp/resp.json
else
  echo "Catalog creation failed: HTTP ${STATUS}"
  cat /tmp/resp.json
  exit 1
fi

# Polaris 1.5's PUT grants is not idempotent: re-PUTting an existing grant trips the
# Postgres grant_records_pkey unique constraint and returns 500 (not 200/409). GET-then-skip.
EXISTING=$(curl -s http://polaris:8181/api/management/v1/catalogs/${CATALOG}/catalog-roles/catalog_admin/grants \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Polaris-Realm: ${REALM}")

if echo "${EXISTING}" | jq -e '.grants[] | select(.type=="catalog" and .privilege=="CATALOG_MANAGE_CONTENT")' > /dev/null; then
  echo "Grant CATALOG_MANAGE_CONTENT->catalog_admin already present, skipping PUT"
else
  GRANT_STATUS=$(curl -s -o /tmp/grant.json -w "%{http_code}" \
    -X PUT http://polaris:8181/api/management/v1/catalogs/${CATALOG}/catalog-roles/catalog_admin/grants \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Polaris-Realm: ${REALM}" \
    -H "Content-Type: application/json" \
    -d '{"type":"catalog","privilege":"CATALOG_MANAGE_CONTENT"}')

  if [ "${GRANT_STATUS}" = "201" ] || [ "${GRANT_STATUS}" = "200" ]; then
    echo "Grant CATALOG_MANAGE_CONTENT->catalog_admin OK (HTTP ${GRANT_STATUS})"
  else
    echo "Grant failed: HTTP ${GRANT_STATUS}"
    cat /tmp/grant.json
    exit 1
  fi
fi
