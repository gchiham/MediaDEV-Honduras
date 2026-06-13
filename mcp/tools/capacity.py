"""
tools/capacity.py — Recursos del host y ancho de banda por stream.
Para decisiones de escalado: ¿aguanta más streams? ¿cuándo desacoplar captura?

get_host_resources()   — CPU, RAM, disco, load + procesos top (incl. ffmpeg).
get_stream_bandwidth() — bitrate real por stream (TV pesa más que radio).
"""
import os
import time
import glob
import shutil
import subprocess
from typing import Any

STREAMS_ROOT     = "/var/www/streams"
HLS_SEG_SECONDS  = 4  # hls_time 4 en stream_run.sh


def _cpu_percent(interval: float = 0.5) -> float:
    def read():
        with open("/proc/stat") as f:
            v = list(map(int, f.readline().split()[1:]))
        idle = v[3] + v[4]  # idle + iowait
        return idle, sum(v)
    i1, t1 = read()
    time.sleep(interval)
    i2, t2 = read()
    dt = (t2 - t1) or 1
    return round((1 - (i2 - i1) / dt) * 100, 1)


def _meminfo() -> dict:
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            m[k] = int(rest.strip().split()[0])  # kB
    return m


def get_host_resources() -> dict[str, Any]:
    """
    Recursos del servidor mediaDEV: CPU, RAM, disco, load y procesos top.
    Incluye el agregado de los ffmpeg de captura.
    """
    cores = os.cpu_count() or 1
    l1, l5, l15 = os.getloadavg()
    m = _meminfo()
    mem_total = m["MemTotal"] // 1024
    mem_avail = m.get("MemAvailable", 0) // 1024
    swap_total = m.get("SwapTotal", 0) // 1024
    swap_used = swap_total - (m.get("SwapFree", 0) // 1024)
    du = shutil.disk_usage("/")
    cpu = _cpu_percent()

    # Procesos top + agregado ffmpeg
    top, ff_cpu, ff_rss, ff_n = [], 0.0, 0, 0
    try:
        r = subprocess.run(["ps", "-eo", "pcpu,pmem,rss,comm", "--sort=-pcpu"],
                           capture_output=True, text=True, timeout=10)
        for ln in r.stdout.splitlines()[1:]:
            parts = ln.split(None, 3)
            if len(parts) < 4:
                continue
            pcpu, pmem, rss, comm = float(parts[0]), float(parts[1]), int(parts[2]), parts[3]
            if comm == "ffmpeg":
                ff_cpu += pcpu; ff_rss += rss; ff_n += 1
            if len(top) < 6:
                top.append({"cmd": comm, "cpu_pct": pcpu, "mem_pct": pmem, "rss_mb": round(rss / 1024, 1)})
    except Exception as e:
        top = [{"error": str(e)}]

    headroom = round(100 - cpu, 1)
    verdict = ("cómodo" if cpu < 60 and mem_avail > mem_total * 0.25
               else "vigilar" if cpu < 85 else "saturado")

    return {
        "host": "mediaDEV",
        "cpu": {"cores": cores, "used_pct": cpu, "idle_pct": headroom,
                "load_1m": round(l1, 2), "load_5m": round(l5, 2), "load_15m": round(l15, 2)},
        "memory_mb": {"total": mem_total, "available": mem_avail, "used": mem_total - mem_avail,
                      "swap_total": swap_total, "swap_used": swap_used},
        "disk_gb": {"total": du.total // (1024**3), "used": du.used // (1024**3),
                    "used_pct": round(du.used / du.total * 100, 1)},
        "ffmpeg": {"procesos": ff_n, "cpu_pct_sumado": round(ff_cpu, 1), "rss_mb_total": round(ff_rss / 1024, 1)},
        "top_procesos": top,
        "veredicto": verdict,
    }


def get_stream_bandwidth() -> dict[str, Any]:
    """
    Bitrate estimado por stream, de los últimos segmentos .ts (HLS 4s).
    El cuello al escalar TV es este (red/disco), no el CPU.
    """
    streams = []
    total = 0.0
    for d in sorted(glob.glob(STREAMS_ROOT + "/*/")):
        sid = os.path.basename(d.rstrip("/"))
        if sid.startswith("_"):
            continue
        segs = sorted(glob.glob(d + "seg_*.ts"), key=os.path.getmtime)[-10:]
        if not segs:
            continue
        sizes = [os.path.getsize(s) for s in segs]
        avg = sum(sizes) / len(sizes)
        mbps = round(avg * 8 / HLS_SEG_SECONDS / 1e6, 2)
        total += mbps
        streams.append({
            "stream": sid,
            "mbps": mbps,
            "tipo": "tv" if mbps > 0.3 else "radio",
            "avg_seg_kb": round(avg / 1024, 1),
            "gb_dia_aprox": round(mbps * 86400 / 8 / 1000, 1),
            "muestras": len(sizes),
        })
    streams.sort(key=lambda x: -x["mbps"])
    tv = [s for s in streams if s["tipo"] == "tv"]
    return {
        "streams": streams,
        "stream_count": len(streams),
        "total_ingest_mbps": round(total, 2),
        "tv_count": len(tv),
        "tv_ingest_mbps": round(sum(s["mbps"] for s in tv), 2),
        "gb_dia_total_aprox": round(total * 86400 / 8 / 1000, 1),
        "nota": "Bitrate de los .ts recientes. TV manda red/disco/S3; radio es barato.",
    }
