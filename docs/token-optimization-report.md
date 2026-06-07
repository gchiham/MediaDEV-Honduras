# Reporte de Optimización de Tokens — MediaDEV

## Jerarquía de contexto creada

```
/opt/media-ai/CLAUDE.md              # Raíz — arquitectura global, constraints, red, DB schema
/opt/media-ai/daemon/CLAUDE.md       # Daemon — intervalos, CB, optimizaciones, pitfalls
/opt/media-ai/dashboard/CLAUDE.md    # Dashboard — queries, timezone, player HLS, pitfalls
/opt/media-ai/scripts/CLAUDE.md      # Scripts — patrones proxy, ffmpeg flags, cambio gateway
/opt/media-ai/README.md              # Para desarrolladores humanos
/opt/media-ai/docs/token-optimization-report.md  # Este archivo
```

## Contexto total sin jerarquía (estimado)
Sin CLAUDE.md, Claude Code necesitaría leer por tarea:
- stream_daemon.py: ~400 líneas (~2,800 tokens)
- dashboard_v4.py: ~230 líneas (~1,600 tokens)
- dashboard_main.html + stream_detail.html: ~600 líneas (~4,200 tokens)
- 12 scripts stream_*.sh: ~600 líneas (~4,200 tokens)
- Nginx, supervisor, systemd configs: ~200 líneas (~1,400 tokens)

**Total sin CLAUDE.md**: ~14,200 tokens por sesión de contexto

## Contexto con jerarquía (estimado)
- CLAUDE.md raíz: ~800 tokens (leído siempre)
- CLAUDE.md específico del módulo: ~400 tokens (leído según tarea)
- Archivos específicos cuando requeridos: ~1,000-2,000 tokens

**Total con CLAUDE.md**: ~2,200 tokens por tarea típica

**Ahorro estimado: 85% de tokens de contexto**

## Documentación duplicada encontrada

| Archivo | Estado | Acción recomendada |
|---|---|---|
| `dashboard_mediadev_v2.py` | Obsoleto | Eliminar cuando se confirme que v4 es estable |
| `dashboard_mediadev_v3.py` | Obsoleto | Eliminar cuando se confirme que v4 es estable |
| `templates/dashboard_mediadev.html` | Obsoleto | Eliminar |
| `templates/dashboard_mediadev_v3.html` | Obsoleto | Eliminar |
| `stream_daemon_new.py` | Duplicado | Eliminar (contenido en stream_daemon.py) |
| `scripts_backup_rpi/` | Backup manual | Conservar hasta que la Pi vuelva online |
| `audit_index/*.csv` y `*.jsonl` | Legado migrado | Eliminar tras confirmar integridad en SQLite |

## Archivos que afectan mayor contexto AI

| Archivo | Líneas | Tokens est. | Nota |
|---|---|---|---|
| `dashboard_main.html` | ~280 | ~3,500 | CSS verbose — considerar externalizar |
| `stream_detail.html` | ~320 | ~4,000 | CSS verbose — considerar externalizar |
| `stream_daemon.py` | ~388 | ~2,800 | Bien estructurado, comentarios claros |
| `dashboard_v4.py` | ~230 | ~1,600 | Bien estructurado |

## Límites de módulos recomendados

```
daemon/      → Modificar SOLO cuando se cambian intervalos, CB logic, o pipeline de grabaciones
dashboard/   → Modificar SOLO cuando se cambia UI, queries, o zona horaria
scripts/     → Modificar SOLO cuando se cambia el gateway o parámetros ffmpeg
infra/       → Cambios en nginx, supervisor, systemd, WireGuard
```

## Estrategia de documentación futura

1. **Mantener CLAUDE.md actualizados** al cambiar intervalos, IPs de gateway, o schema DB
2. **No duplicar** información entre CLAUDE.md padre e hijo
3. **Agregar pitfalls** a medida que se descubren durante el desarrollo
4. **Nunca documentar** código obvio — solo decisiones no-evidentes y sus razones
5. **Actualizar la tabla de gateways** en scripts/CLAUDE.md cuando cambie el nodo de salida

## Convención de commits recomendada
```
feat(daemon): descripción
fix(dashboard): descripción
infra(wireguard): descripción
docs(claude): descripción
```

## Sesiones futuras con Claude Code — recomendaciones

1. Abrir siempre con: "Lee /opt/media-ai/CLAUDE.md primero"
2. Para tareas del daemon: "Lee también /opt/media-ai/daemon/CLAUDE.md"
3. Para tareas del dashboard: "Lee también /opt/media-ai/dashboard/CLAUDE.md"
4. Antes de cambiar intervalos: siempre mencionar la constraint "1 vCPU / 2GB RAM"
5. Antes de cambiar queries SQL: mencionar "usar GROUP BY batch, no loops individuales"
6. El constraint de hardware es la decisión arquitectónica más importante del sistema
