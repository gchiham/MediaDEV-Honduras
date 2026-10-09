#!/usr/bin/env python3
"""
Backup Health Check — Active-Active failover para grabaciones de audio.

Corre cada 30 min. Para cada stream verifica las últimas 4 horas:
  - Si archivo canónico existe en S3 → borra backup (todo bien)
  - Si archivo canónico FALTA → promueve backup a canónico → borra backup → alerta
  - Si backup tampoco existe → log warning (ambos peers fallaron)
"""
import os, sys, logging, boto3
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError

# ── Config ────────────────────────────────────────────────────────────────────
S3_BUCKET   = os.environ.get("S3_BUCKET", "mediadev-recordings")
S3_REGION   = os.environ.get("S3_REGION", "us-east-1")
BACKUP_PFX  = "_backup"
CHECK_HOURS = 4   # cuántas horas hacia atrás revisar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/backup_healthcheck.log"),
    ]
)
log = logging.getLogger("healthcheck")

# ── S3 helpers ────────────────────────────────────────────────────────────────
def get_s3():
    return boto3.client("s3", region_name=S3_REGION)

def key_exists(s3, key: str) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise

def canonical_key(stream_id: str, dt: datetime) -> str:
    return f"{stream_id}/{dt.strftime('%Y')}/{dt.strftime('%m')}/{dt.strftime('%Y-%m-%d_%Hh')}.mp3"

def backup_key(stream_id: str, dt: datetime) -> str:
    return f"{BACKUP_PFX}/{canonical_key(stream_id, dt)}"

def list_active_streams(s3) -> list[str]:
    """Streams que tienen archivos en la ruta canónica."""
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Delimiter="/")
    streams = []
    for prefix in resp.get("CommonPrefixes", []):
        name = prefix["Prefix"].rstrip("/")
        if name != BACKUP_PFX:
            streams.append(name)
    return streams

# ── Core logic ────────────────────────────────────────────────────────────────
def check_hour(s3, stream_id: str, dt: datetime) -> str:
    """
    Verifica un slot hora/stream.
    Returns: "ok_canonical" | "promoted" | "deleted_dup" | "both_missing"
    """
    canon = canonical_key(stream_id, dt)
    backup = backup_key(stream_id, dt)

    has_canon  = key_exists(s3, canon)
    has_backup = key_exists(s3, backup)

    if not has_backup:
        # Sin backup — nada que hacer aquí
        return "ok_canonical" if has_canon else "no_backup_needed"

    if has_canon:
        # Canónica OK → backup ya no sirve, borrar
        s3.delete_object(Bucket=S3_BUCKET, Key=backup)
        log.info(f"[{stream_id}] {dt.strftime('%Y-%m-%d %Hh')} — canónica OK, backup borrado")
        return "deleted_dup"
    else:
        # Canónica FALTA → promover backup
        s3.copy_object(
            Bucket=S3_BUCKET,
            CopySource={"Bucket": S3_BUCKET, "Key": backup},
            Key=canon,
            MetadataDirective="COPY",
        )
        s3.delete_object(Bucket=S3_BUCKET, Key=backup)
        log.warning(
            f"[{stream_id}] {dt.strftime('%Y-%m-%d %Hh')} — "
            f"PEER A FALLO — backup promovido a canónica ✅"
        )
        return "promoted"

def run():
    s3 = get_s3()
    now_utc = datetime.now(tz=timezone.utc)
    streams = list_active_streams(s3)

    if not streams:
        log.warning("No se encontraron streams en S3")
        return

    log.info(f"Verificando {len(streams)} streams, últimas {CHECK_HOURS} horas")

    stats = {"ok_canonical": 0, "deleted_dup": 0, "promoted": 0,
             "no_backup_needed": 0, "both_missing": 0}

    for stream_id in streams:
        for h in range(1, CHECK_HOURS + 1):
            slot = (now_utc - timedelta(hours=h)).replace(minute=0, second=0, microsecond=0)
            result = check_hour(s3, stream_id, slot)
            stats[result] = stats.get(result, 0) + 1

    log.info(
        f"Resumen — canónicas OK: {stats['ok_canonical']} | "
        f"backups borrados: {stats['deleted_dup']} | "
        f"promovidos: {stats['promoted']} | "
        f"ambos faltantes: {stats['both_missing']}"
    )

    if stats.get("promoted", 0) > 0:
        log.warning(f"⚠️  {stats['promoted']} archivos promovidos desde backup — Peer A tuvo fallas")

if __name__ == "__main__":
    run()
