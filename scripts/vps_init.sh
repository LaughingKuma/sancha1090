#!/bin/bash
# Cloud-init user-data for the opensky-collector VPS.
# vps_up.sh substitutes ${ENV_FILE_CONTENTS} before sending to Hetzner.
set -euxo pipefail

apt-get update
apt-get install -y --no-install-recommends python3-venv python3-pip ca-certificates

useradd -r -m -s /usr/sbin/nologin opensky || true
install -d -o opensky -g opensky -m 0750 /opt/opensky

cat >/etc/opensky.env <<'ENV_EOF'
${ENV_FILE_CONTENTS}
ENV_EOF
chmod 0600 /etc/opensky.env
chown root:opensky /etc/opensky.env

python3 -m venv /opt/opensky/.venv
/opt/opensky/.venv/bin/pip install --no-cache-dir \
    "httpx==0.28.1" "polars==1.40.1" "pyarrow==24.0.0"

cat >/opt/opensky/vps_collector.py <<'PY_EOF'
${COLLECTOR_PAYLOAD}
PY_EOF
chown opensky:opensky /opt/opensky/vps_collector.py
chmod 0755 /opt/opensky/vps_collector.py

cat >/etc/systemd/system/opensky-collector.service <<'SVC_EOF'
[Unit]
Description=OpenSky VPS collector (one-shot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=opensky
EnvironmentFile=/etc/opensky.env
ExecStart=/opt/opensky/.venv/bin/python /opt/opensky/vps_collector.py
SVC_EOF

cat >/etc/systemd/system/opensky-collector.timer <<'TMR_EOF'
[Unit]
Description=Run opensky-collector every 12 minutes
[Timer]
OnCalendar=*:0/12:00
Persistent=true
AccuracySec=10s
[Install]
WantedBy=timers.target
TMR_EOF

systemctl daemon-reload
systemctl enable --now opensky-collector.timer
# Fire one immediate run so we don't wait up to 12 min on first boot.
systemctl start opensky-collector.service || true
