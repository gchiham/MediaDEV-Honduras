"""
Configuration for MediaDEV Telegram Monitoring
"""

import os
from typing import Dict

# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv("MEDIADEV_BOT_TOKEN", "TEMPORARY_TOKEN_PLACEHOLDER")
TELEGRAM_CHAT_ID = int(os.getenv("MEDIADEV_CHAT_ID", "-5290139816"))

# WireGuard Configuration
WG_INTERFACE = "wg0"
WG_CONFIG_PATH = "/etc/wireguard/wg0.conf"

# Peer Configuration with metadata
PEERS = {
    "Xt4N/0OY6UpqfZYY+cz2OVcV3wQRKJuf30DAbHDm5ng=": {
        "name": "RPi-HN01", "ip": "10.101.0.2", "role": "Gateway (Standby)"
    },
    "WeJuHENAEsRFwjT2phg1Vn5K3i6oTZiDFs6Wh35Kf3Q=": {
        "name": "PC-Sedesol", "ip": "10.101.0.3", "role": "Development"
    },
        "name": "PC-Developer", "ip": "10.101.0.4", "role": "Development"
    },
    "ESXXCvlAC0a+RQhbBdAcIftxc2E+2E0IU9VRmIiDOVs=": {
        "name": "PC-LCE", "ip": "10.101.0.5", "role": "Gateway (Active)"
    },
    "iLWsb6GyE2bys9XOkiBUPXlx1mtxhz18TXjDmkFFjWk=": {
        "name": "RPi-Levi", "ip": "10.101.0.6", "role": "Gateway (Standby)"
    },
}

# Monitoring Configuration
HANDSHAKE_TIMEOUT_SECONDS = 180  # Peer is OFFLINE if no handshake for 3 minutes
MONITOR_CHECK_INTERVAL = 60      # Check status every 60 seconds
ALERT_COOLDOWN = 300             # Prevent duplicate alerts for 5 minutes

# Database Configuration
DATABASE_PATH = "/opt/media-ai/monitor/events.db"

# Logging Configuration
LOG_DIR = "/opt/media-ai/monitor/logs"
LOG_LEVEL = "INFO"
BOT_LOG_FILE = f"{LOG_DIR}/telegram_bot.log"
MONITOR_LOG_FILE = f"{LOG_DIR}/monitor.log"
SYSTEM_LOG_FILE = f"{LOG_DIR}/system.log"

# Server Configuration
SERVER_NAME = "MediaDEV"
SERVER_IP = "159.223.104.91"

# Timezone
TIMEZONE = "America/Tegucigalpa"

# Feature Flags
ENABLE_ALERTS = True
ENABLE_LOGGING = True
ENABLE_DATABASE = True

# Maximum message length for Telegram
MAX_MESSAGE_LENGTH = 4096

# Commands available
AVAILABLE_COMMANDS = [
    "/status",
    "/wg",
    "/alerts",
    "/streams",
    "/help"
]
