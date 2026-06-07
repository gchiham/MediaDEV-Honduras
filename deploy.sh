#!/bin/bash
# Deploy script — sincroniza cambios de GitHub y reinicia servicios

set -e

LOG="/var/log/streams/deploy.log"
cd /opt/media-ai

echo "[$(date)]Deploy iniciado" >> $LOG

# 1. Actualizar código desde GitHub
echo "Pulling from GitHub..."
git fetch origin main 2>&1 | tee -a $LOG
git reset --hard origin/main 2>&1 | tee -a $LOG

# 2. Detectar qué cambió
CHANGED=$(git diff HEAD~1 HEAD --name-only 2>/dev/null || echo "all")
echo "Cambios: $CHANGED" | tee -a $LOG

# 3. Reiniciar servicios si cambiaron archivos críticos
if echo "$CHANGED" | grep -qE "^(daemon|dashboard|scripts)/"; then
    echo "→ Reiniciando servicios" | tee -a $LOG
    
    if echo "$CHANGED" | grep -q "^daemon/"; then
        systemctl restart stream-daemon
    fi
    
    if echo "$CHANGED" | grep -q "^dashboard/"; then
        systemctl restart dashboard-mediadev
    fi
    
    if echo "$CHANGED" | grep -q "^scripts/"; then
        supervisorctl restart all
    fi
fi

echo "[$(date)] Deploy OK" >> $LOG
