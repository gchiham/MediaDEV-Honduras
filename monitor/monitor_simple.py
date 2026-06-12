
import subprocess, time, logging
from config import PEERS, HANDSHAKE_TIMEOUT_SECONDS, MONITOR_CHECK_INTERVAL
from wireguard import WireGuardMonitor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

monitor = WireGuardMonitor()
peer_states = {}

while True:
    for peer_key in PEERS:
        is_online = monitor.is_peer_online(peer_key, HANDSHAKE_TIMEOUT_SECONDS)
        if peer_key not in peer_states:
            peer_states[peer_key] = is_online
        elif peer_states[peer_key] != is_online:
            logger.warning(f"Peer {PEERS[peer_key]["name"]} changed to {"ONLINE" if is_online else "OFFLINE"}")
            peer_states[peer_key] = is_online
    time.sleep(MONITOR_CHECK_INTERVAL)

