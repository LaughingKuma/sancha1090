#!/bin/bash
# Cloud-init user-data for the ${PROJECT_NAME}-collector VPS.
# vps_up.sh substitutes envsubst placeholders before sending to Hetzner.
set -euxo pipefail

apt-get update
apt-get install -y --no-install-recommends python3-venv python3-pip ca-certificates

useradd -r -m -s /usr/sbin/nologin ${PROJECT_NAME} || true
install -d -o ${PROJECT_NAME} -g ${PROJECT_NAME} -m 0750 /opt/${PROJECT_NAME}

cat >/etc/${PROJECT_NAME}.env <<'ENV_EOF'
${ENV_FILE_CONTENTS}
ENV_EOF
chmod 0600 /etc/${PROJECT_NAME}.env
chown root:${PROJECT_NAME} /etc/${PROJECT_NAME}.env

python3 -m venv /opt/${PROJECT_NAME}/.venv
/opt/${PROJECT_NAME}/.venv/bin/pip install --no-cache-dir \
    "httpx==0.28.1" "polars==1.40.1" "pyarrow==24.0.0"

cat >/opt/${PROJECT_NAME}/vps_collector.py <<'PY_EOF'
${COLLECTOR_PAYLOAD}
PY_EOF
chown ${PROJECT_NAME}:${PROJECT_NAME} /opt/${PROJECT_NAME}/vps_collector.py
chmod 0755 /opt/${PROJECT_NAME}/vps_collector.py

cat >/etc/systemd/system/${PROJECT_NAME}-collector.service <<'SVC_EOF'
[Unit]
Description=${PROJECT_NAME} VPS collector (one-shot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${PROJECT_NAME}
EnvironmentFile=/etc/${PROJECT_NAME}.env
ExecStart=/opt/${PROJECT_NAME}/.venv/bin/python /opt/${PROJECT_NAME}/vps_collector.py
SVC_EOF

cat >/etc/systemd/system/${PROJECT_NAME}-collector.timer <<'TMR_EOF'
[Unit]
Description=Run ${PROJECT_NAME}-collector every 12 minutes
[Timer]
OnCalendar=*:0/12:00
Persistent=true
AccuracySec=10s
[Install]
WantedBy=timers.target
TMR_EOF

systemctl daemon-reload
systemctl enable --now ${PROJECT_NAME}-collector.timer
# Fire one immediate run so we don't wait up to 12 min on first boot.
systemctl start ${PROJECT_NAME}-collector.service || true
