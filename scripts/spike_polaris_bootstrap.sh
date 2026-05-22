#!/bin/sh
set -u

out=$(java -jar /deployments/polaris-admin-tool.jar \
  bootstrap \
  --realm=POLARIS \
  --credential="POLARIS,${POLARIS_ROOT_CLIENT_ID},${POLARIS_ROOT_CLIENT_SECRET}" 2>&1)
rc=$?
echo "$out"
if [ $rc -eq 0 ]; then
  exit 0
fi
if echo "$out" | grep -q "already been bootstrapped"; then
  echo "spike-bootstrap: realm POLARIS already bootstrapped, treating as success"
  exit 0
fi
exit $rc
