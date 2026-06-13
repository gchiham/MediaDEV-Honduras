"""
tools/cost.py — Analítica de costo y confiabilidad.

get_destroyer_analytics() — corridas del Destroyer: boot/work/costo, detección de cuelgues.
get_droplets()            — inventario de droplets DO, caza droplets Destroyer huérfanos.
"""
import os
import json
import urllib.request
from typing import Any

from db import qall

C16_HOURLY_USD = 0.952  # estimado c-16 (16 vCPU / 32 GB) CPU-optimized


def get_destroyer_analytics(limit: int = 12) -> dict[str, Any]:
    """
    Analítica de las corridas del Destroyer para decidir confiabilidad y frecuencia.

    Detecta el patrón de cuelgue (timeout con 0 detecciones = el worker se trabó),
    y resume boot vs trabajo vs costo. Powers: tuning de frecuencia (4→6 corridas/día)
    y diagnóstico de estabilidad.
    """
    limit = min(max(1, limit), 50)
    runs = qall("""
        SELECT id, status, files_done, total_files, total_detections,
               boot_seconds, work_seconds, cost_usd, t2_started
        FROM destroyer_runs
        WHERE t2_started IS NOT NULL
        ORDER BY id DESC LIMIT %s
    """, (limit,))

    def fmt(r):
        return {
            "id": r["id"], "status": r["status"],
            "progreso": f'{r["files_done"]}/{r["total_files"]}',
            "detecciones": r["total_detections"],
            "boot_s": r["boot_seconds"], "work_s": r["work_seconds"],
            "costo_usd": round(float(r["cost_usd"]), 3) if r["cost_usd"] is not None else None,
        }

    timeouts = [r for r in runs if r["status"] == "timeout"]
    stalls   = [r for r in runs if r["status"] == "timeout" and (r["total_detections"] or 0) == 0]
    done     = [r for r in runs if r["status"] in ("done", "destroyed")]

    boots = [r["boot_seconds"] for r in done if r["boot_seconds"]]
    works = [r["work_seconds"] for r in done if r["work_seconds"]]
    costs = [float(r["cost_usd"]) for r in runs if r["cost_usd"] is not None]

    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None

    if stalls:
        verdict = (f"PROBLEMA DE CONFIABILIDAD: {len(stalls)} corrida(s) timeout con 0 detecciones "
                   f"— el worker se cuelga en lotes grandes. Revisar timeout por archivo antes de tunear frecuencia.")
    elif timeouts:
        verdict = f"{len(timeouts)} timeout(s) recientes — vigilar."
    else:
        verdict = "Corridas estables."

    return {
        "runs": [fmt(r) for r in runs],
        "resumen": {
            "total_analizadas": len(runs),
            "completadas": len(done),
            "timeouts": len(timeouts),
            "cuelgues_0_detecciones": len(stalls),
            "boot_promedio_s": avg(boots),
            "trabajo_promedio_s": avg(works),
            "costo_promedio_usd": round(sum(costs) / len(costs), 3) if costs else None,
        },
        "veredicto": verdict,
    }


def _load_do_token() -> str | None:
    try:
        with open("/opt/destroyer/.env") as f:
            for line in f:
                if line.startswith("DO_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    return os.environ.get("DO_TOKEN")


def get_droplets() -> dict[str, Any]:
    """
    Inventario de droplets en DigitalOcean + caza de droplets Destroyer huérfanos.

    Los droplets con tag 'destroyer'/'destroyer-build' deben ser EFÍMEROS. Si aparecen
    'active', es que un worker se colgó y no se auto-destruyó (verificar el watchdog) —
    están gastando ~$0.95/hora cada uno.
    """
    tok = _load_do_token()
    if not tok:
        return {"error": "DO_TOKEN no disponible"}
    try:
        req = urllib.request.Request(
            "https://api.digitalocean.com/v2/droplets?per_page=100",
            headers={"Authorization": "Bearer " + tok})
        data = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception as e:
        return {"error": str(e)}

    drs, orphans = [], []
    for d in data.get("droplets", []):
        tags = d.get("tags", [])
        info = {"id": d["id"], "name": d["name"], "status": d["status"],
                "size": d["size_slug"], "created_at": d["created_at"], "tags": tags}
        drs.append(info)
        if d["status"] == "active" and ("destroyer" in tags or "destroyer-build" in tags):
            orphans.append(info)

    waste = round(len(orphans) * C16_HOURLY_USD, 2)
    return {
        "total": len(drs),
        "droplets": drs,
        "droplets_destroyer_huerfanos": orphans,
        "warning": (f"{len(orphans)} droplet(s) Destroyer activos que deberían ser efímeros "
                    f"— ~${waste}/hora desperdiciados. Revisar el cron del watchdog.") if orphans else None,
    }
