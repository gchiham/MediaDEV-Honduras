#!/usr/bin/env python3
"""
Migración a SQLite: importa history.db (CSV) y audit_index/*.jsonl
al nuevo schema en mediadev.db
"""
import sqlite3, csv, json, os, time
from pathlib import Path

DB_PATH      = "/var/www/streams/mediadev.db"
STREAMS_ROOT = Path("/var/www/streams")
HISTORY_CSV  = "/var/www/streams/metrics/history.db"
INDEX_DIR    = STREAMS_ROOT / "audit_index"

def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db

def migrate_history(db):
    if not Path(HISTORY_CSV).exists():
        print("history.db no encontrado, saltando")
        return
    count = 0
    with open(HISTORY_CSV) as f:
        for row in csv.DictReader(f, delimiter="|"):
            try:
                from datetime import datetime, timezone
                ts = int(datetime.strptime(
                    row["timestamp"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc).timestamp())
                ts = ts - ts % 60
                db.execute(
                    "INSERT OR IGNORE INTO metrics (stream_id,ts,status,segs,bytes) VALUES (?,?,?,?,?)",
                    (row["stream_id"], ts, row["status"], int(row.get("segs",0)), 0)
                )
                count += 1
            except:
                pass
    db.commit()
    print(f"Métricas importadas: {count} filas")

def migrate_segments(db):
    if not INDEX_DIR.exists():
        print("audit_index no encontrado, saltando")
        return
    count = 0
    for jsonl in INDEX_DIR.glob("*.jsonl"):
        sid = jsonl.stem
        for line in jsonl.read_text().splitlines():
            try:
                e = json.loads(line)
                seg_path = STREAMS_ROOT / sid / e["seg"]
                size = seg_path.stat().st_size if seg_path.exists() else 0
                db.execute(
                    "INSERT OR IGNORE INTO segments (stream_id,filename,ts_start,ts_end,bytes) VALUES (?,?,?,?,?)",
                    (sid, e["seg"], e["start"], e["end"], size)
                )
                count += 1
            except:
                pass
    db.commit()
    print(f"Segmentos importados: {count}")

def migrate_status(db):
    status_file = STREAMS_ROOT / "status.json"
    if not status_file.exists():
        return
    try:
        d = json.loads(status_file.read_text())
        for sid, info in d.get("streams", {}).items():
            db.execute("""
                UPDATE stream_status SET
                    status=?, sup=?, segs=?, age=?, updated_at=?
                WHERE stream_id=?
            """, (info.get("status","UNKNOWN"), info.get("sup","UNKNOWN"),
                  info.get("segs",0), info.get("age",0),
                  int(time.time()), sid))
        db.commit()
        print("Estado actual importado")
    except Exception as e:
        print(f"Error importando status: {e}")

if __name__ == "__main__":
    db = connect()
    print("Iniciando migración a SQLite...")
    migrate_history(db)
    migrate_segments(db)
    migrate_status(db)
    # Verificar
    n_met = db.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    n_seg = db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    print(f"\n✓ Migración completa: {n_met} métricas, {n_seg} segmentos")
    db.close()
