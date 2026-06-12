#!/bin/bash
# ============================================================
# deploy_peer_b.sh — Configura Peer B (grabador de respaldo)
# Ejecutar en el NUEVO droplet como root
# ============================================================
set -euo pipefail

PEER_A_IP="159.223.104.91"
MEDIAAI_DIR="/opt/media-ai"
STREAMS_DIR="/var/www/streams"

echo "=== Peer B Setup ==="

# 1. Dependencias
apt-get update -qq
apt-get install -y -qq ffmpeg python3 python3-pip nginx curl

pip3 install -q boto3

# 2. Copiar código desde Peer A (ejecutar con acceso SSH a Peer A)
echo "Copiando media-ai desde Peer A..."
rsync -az --exclude='__pycache__' --exclude='*.pyc' \
    root@${PEER_A_IP}:/opt/media-ai/ /opt/media-ai/

# 3. Crear directorios de streams
mkdir -p ${STREAMS_DIR}/audit_index

# Los mismos streams que Peer A
STREAMS="radio_america radio_choluteca suave_fm xy_tgu radio_satelite hch_tv teleceiba cbx_fm sfl_honduras"
for s in $STREAMS; do
    mkdir -p ${STREAMS_DIR}/${s}
done

# 4. Env con PEER_ROLE=backup
cat > /etc/mediadev-s3.env << 'EOF'
AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}
S3_BUCKET=mediadev-recordings
S3_REGION=us-east-1
PEER_ROLE=backup
EOF
chmod 600 /etc/mediadev-s3.env

# 5. Servicio systemd (igual que Peer A)
cat > /etc/systemd/system/stream-daemon.service << 'EOF'
[Unit]
Description=MediaDEV Stream Daemon (Peer B — Backup)
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/media-ai/daemon/stream_daemon.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/mediadev-s3.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 6. Health check en Peer B también (por si Peer A no puede correrlo)
cp /tmp/backup_healthcheck.py /opt/media-ai/scripts/backup_healthcheck.py
chmod +x /opt/media-ai/scripts/backup_healthcheck.py

# Cron cada 30 min
(crontab -l 2>/dev/null; echo "*/30 * * * * /usr/bin/python3 /opt/media-ai/scripts/backup_healthcheck.py >> /var/log/backup_healthcheck.log 2>&1") | crontab -

# 7. Arrancar
systemctl daemon-reload
systemctl enable stream-daemon
systemctl start stream-daemon

echo ""
echo "=== Peer B listo ==="
echo "PEER_ROLE=backup — subiendo a s3://mediadev-recordings/_backup/"
echo "Health check corriendo cada 30 min"
systemctl status stream-daemon --no-pager | head -6
