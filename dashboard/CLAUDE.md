# Dashboard — CLAUDE.md

## Propósito
Aplicación Flask que sirve el dashboard web y la API JSON.
Gunicorn con 1 worker (constraint 1 vCPU) detrás de nginx en puerto 80.

## Archivos clave
- `dashboard_v4.py` — versión activa, única que modificar
- `dashboard_v4.py.bak_utc` — backup pre-cambio de timezone
- `dashboard_mediadev_v2.py`, `dashboard_mediadev_v3.py` — versiones obsoletas
- `templates/dashboard_main.html` — 6 KPIs globales + grid 12 stream cards
- `templates/stream_detail.html` — detalle por stream: player HLS, KPIs, gráfico, auditoría, eventos

## Rutas
- `GET /` — Dashboard principal
- `GET /stream/<sid>` — Detalle de stream individual
- `GET /api/status` — JSON con estado de todos los streams
- `GET /streams/<sid>/index.m3u8` — Playlist HLS (nginx directo, no Flask)

## Regla de queries — SIEMPRE batch GROUP BY
```python
# CORRECTO — 1 query para todos los streams
m24 = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
    "SELECT stream_id, COUNT(*), SUM(CASE WHEN status='OK' THEN 1 ELSE 0 END), SUM(bytes)"
    " FROM metrics WHERE ts>=? GROUP BY stream_id", (cutoff,))}

# INCORRECTO — 72 queries (causa TTFB 6s)
for sid in STREAMS:
    con.execute("SELECT COUNT(*) FROM metrics WHERE stream_id=? AND ts>=?", (sid, cutoff))
```

## Zona horaria
```python
TGU = timezone(timedelta(hours=-6))  # America/Tegucigalpa GMT-6
# Usar TGU en TODOS los datetime.fromtimestamp() y datetime.now()
# NUNCA timezone.utc en código de presentación
```

## Player HLS
- HLS.js v1.5.7 con elemento `<video>` (no `<audio>` — HLS.js lo requiere internamente)
- Carga bajo demanda al hacer click en play
- Auto-play solo cuando status == "ok"

## Servicio
```bash
systemctl restart dashboard-mediadev
journalctl -u dashboard-mediadev -f
```

## Pitfalls conocidos
- NO usar `PRAGMA query_only=ON` — rompe row_factory, rows devuelven tuplas en vez de sqlite3.Row
- Variables de loop de chart: usar `hr` no `row` (evita sobreescribir la query outer)
- Llamar `last_event_str(con, sid, etype)` ANTES de `con.close()`
- `template_folder="templates"` no `template_folder="."`
