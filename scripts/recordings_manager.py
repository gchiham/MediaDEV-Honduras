#!/usr/bin/env python3
"""
Gestiona grabaciones horarias de audio para auditoría.
- Indexa segmentos .ts con timestamps reales
- Genera MP3 de 1 hora a partir de los segmentos existentes
- Mantiene 8 horas de grabaciones por stream
"""
import os, sys, glob, json, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

STREAMS_DIR   = Path("/var/www/streams")
INDEX_DIR     = Path("/var/www/streams/audit_index")
SKIP_DIRS     = {"metrics", "audit_index"}
SEG_DURATION  = 4       # segundos por segmento
KEEP_HOURS    = 8       # horas a retener

INDEX_DIR.mkdir(parents=True, exist_ok=True)

def get_stream_dirs():
    return [d for d in STREAMS_DIR.iterdir()
            if d.is_dir() and d.name not in SKIP_DIRS]

# ── INDEXER ──────────────────────────────────────────────────────────────────

def index_stream(stream_dir: Path):
    """Indexa segmentos nuevos en audit_index/<stream>.jsonl"""
    stream_id  = stream_dir.name
    index_file = INDEX_DIR / f"{stream_id}.jsonl"

    # Cargar índice existente
    indexed = {}
    if index_file.exists():
        for line in index_file.read_text().splitlines():
            try:
                e = json.loads(line)
                indexed[e["seg"]] = e
            except: pass

    # Segmentos en disco
    segs = sorted(stream_dir.glob("seg_*.ts"))
    new_entries = []
    for seg in segs:
        name = seg.name
        if name in indexed:
            continue
        try:
            mtime     = seg.stat().st_mtime
            end_epoch = int(mtime)
            beg_epoch = end_epoch - SEG_DURATION
            new_entries.append({
                "seg":       name,
                "start":     beg_epoch,
                "end":       end_epoch,
                "start_ts":  datetime.fromtimestamp(beg_epoch, tz=timezone.utc)
                               .strftime("%Y-%m-%d %H:%M:%S"),
                "end_ts":    datetime.fromtimestamp(end_epoch, tz=timezone.utc)
                               .strftime("%Y-%m-%d %H:%M:%S"),
            })
        except: pass

    if new_entries:
        with open(index_file, "a") as f:
            for e in new_entries:
                f.write(json.dumps(e) + "\n")

    # Limpiar entradas de archivos que ya no existen
    existing_names = {s.name for s in segs}
    cleaned = [v for k, v in indexed.items() if k in existing_names]
    cleaned += new_entries
    index_file.write_text("\n".join(json.dumps(e) for e in cleaned) + "\n")

    return len(new_entries)

# ── HOURLY RECORDER ──────────────────────────────────────────────────────────

def make_hourly_mp3(stream_dir: Path, hour_dt: datetime):
    """
    Genera un MP3 con los segmentos de la hora indicada.
    hour_dt debe ser UTC con minutos=0, segundos=0.
    """
    stream_id  = stream_dir.name
    index_file = INDEX_DIR / f"{stream_id}.jsonl"
    rec_dir    = stream_dir / "recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)

    hour_label = hour_dt.strftime("%Y-%m-%d_%Hh")
    output_mp3 = rec_dir / f"{hour_label}.mp3"

    if output_mp3.exists():
        return None  # Ya existe

    if not index_file.exists():
        return "sin_indice"

    start_epoch = int(hour_dt.timestamp())
    end_epoch   = start_epoch + 3600

    # Recolectar segmentos en rango
    segs = []
    for line in index_file.read_text().splitlines():
        try:
            e = json.loads(line)
            if e["start"] >= start_epoch and e["start"] < end_epoch:
                seg_path = stream_dir / e["seg"]
                if seg_path.exists():
                    segs.append((e["start"], seg_path))
        except: pass

    segs.sort(key=lambda x: x[0])
    if len(segs) < 10:
        return f"pocos_segmentos({len(segs)})"

    minutes = len(segs) * SEG_DURATION // 60
    print(f"  [{stream_id}] {hour_label}: {len(segs)} segs (~{minutes} min) → {output_mp3.name}")

    # Concatenar y convertir con ffmpeg
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", "pipe:0",
         "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "1", "-ar", "22050",
         str(output_mp3)],
        input=b"".join(seg_path.read_bytes() for _, seg_path in segs),
        capture_output=True
    )

    if proc.returncode == 0:
        size_mb = output_mp3.stat().st_size / 1048576
        return f"ok ({size_mb:.1f} MB)"
    else:
        output_mp3.unlink(missing_ok=True)
        return f"error_ffmpeg"

def cleanup_recordings(stream_dir: Path, keep=KEEP_HOURS):
    """Elimina MP3 más viejos que keep grabaciones."""
    rec_dir = stream_dir / "recordings"
    if not rec_dir.exists():
        return
    mp3s = sorted(rec_dir.glob("*.mp3"), key=lambda f: f.name, reverse=True)
    for old in mp3s[keep:]:
        old.unlink()
        print(f"  [{stream_dir.name}] Eliminado: {old.name}")

# ── MAIN ─────────────────────────────────────────────────────────────────────

def cmd_index():
    """Indexa todos los streams."""
    total = 0
    for d in get_stream_dirs():
        n = index_stream(d)
        if n: print(f"  [{d.name}] +{n} segmentos indexados")
        total += n
    print(f"Total: {total} segmentos nuevos indexados")

def cmd_record(target_hour=None):
    """Genera grabaciones de la hora anterior (o target_hour)."""
    if target_hour is None:
        # Hora anterior completa
        now = datetime.now(tz=timezone.utc)
        target_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)

    print(f"\n[{datetime.now(tz=timezone.utc).strftime('%H:%M:%S')}] "
          f"Grabaciones para: {target_hour.strftime('%Y-%m-%d %Hh UTC')}")

    for d in get_stream_dirs():
        result = make_hourly_mp3(d, target_hour)
        if result and result != "sin_indice":
            print(f"  [{d.name}] {result}")
        cleanup_recordings(d)

def cmd_status():
    """Muestra estado de grabaciones por stream."""
    print(f"\n{STREAM:<22} {GRABACIONES:<12} {HORAS:<8} {TAMAÑO}")
    print("-" * 60)
    for d in sorted(get_stream_dirs(), key=lambda x: x.name):
        rec_dir = d / "recordings"
        mp3s = sorted(rec_dir.glob("*.mp3")) if rec_dir.exists() else []
        total_mb = sum(f.stat().st_size for f in mp3s) / 1048576
        oldest = mp3s[0].stem if mp3s else "—"
        newest = mp3s[-1].stem if mp3s else "—"
        print(f"  {d.name:<20} {len(mp3s):<12} {len(mp3s):<8} {total_mb:.1f} MB")
    print()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "index"
    if cmd == "index":
        cmd_index()
    elif cmd == "record":
        cmd_record()
    elif cmd == "status":
        cmd_status()
    elif cmd == "all":
        cmd_index()
        cmd_record()
    else:
        print(f"Uso: {sys.argv[0]} [index|record|status|all]")
