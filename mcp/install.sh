#!/usr/bin/env bash
# install.sh — Instala y registra mediadev-mcp como servicio systemd
# Uso: bash install.sh

set -euo pipefail

MCP_DIR="/opt/media-ai/mcp"
VENV="$MCP_DIR/venv"
PYTHON="$VENV/bin/python"

echo "=== mediadev-mcp install ==="

# 1. Instalar dependencias
echo "[1/3] Instalando dependencias Python..."
"$PYTHON" -m pip install --quiet -r "$MCP_DIR/requirements.txt"

# 2. Verificar importaciones
echo "[2/3] Verificando importaciones..."
"$PYTHON" -c "
from mcp.server.fastmcp import FastMCP
import psycopg2
print('  mcp:', __import__('mcp').__version__)
print('  psycopg2: OK')
print('  Todas las dependencias disponibles')
"

# 3. Smoke test de las tools (sin DB — solo estructura)
echo "[3/3] Verificando estructura del servidor..."
"$PYTHON" -c "
import sys; sys.path.insert(0, '/opt/media-ai/mcp')
from tools.system import get_system_status
from tools.workers import get_workers
from tools.queue import get_queue_stats
from tools.health import get_service_health
from tools.errors import get_recent_errors
print('  Todas las tools importadas correctamente')
"

echo ""
echo "=== Instalación completa ==="
echo ""
echo "Para usar con Claude Code, agregar en ~/.claude/claude_desktop_config.json:"
echo '{'
echo '  "mcpServers": {'
echo '    "mediadev": {'
echo '      "command": "ssh",'
echo '      "args": ["-i", "~/.ssh/keySED", "-o", "StrictHostKeyChecking=no",'
echo '               "root@159.223.104.91",'
echo '               "/opt/media-ai/mcp/venv/bin/python",'
echo '               "/opt/media-ai/mcp/server.py"]'
echo '    }'
echo '  }'
echo '}'
