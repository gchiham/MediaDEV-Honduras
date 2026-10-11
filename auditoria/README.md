# auditoria/ — auditoría de código de mediaCAP

Corre en GitHub Actions (`.github/workflows/auditoria.yml`) en cada push a `main`, en cada PR y
a mano (Actions → *Auditoría mediaCAP* → *Run workflow*). Solo lee el repo: no tiene secretos
ni acceso a mediaCAP, mediaAPP o la DB.

| Job | Qué revisa | ¿Bloquea? |
|---|---|---|
| Reglas mediaCAP | `reglas_mediacap.py`: patrones de incidentes reales | Sí, si hay CONFIRMED |
| Secretos | gitleaks sobre el árbol actual | Sí |
| Secretos (historial) | gitleaks sobre todos los commits | No, solo reporta |
| Ruff + Bandit | bugs, fechas sin zona, excepciones tragadas, seguridad | No, solo reporta |
| Tests existentes | `tests/` con ffmpeg | No, hasta ver que son estables en CI |

El resultado aparece en el *Summary* de cada corrida y como artefactos JSON.

## Reglas

| Regla | Incidente de origen | Estado |
|---|---|---|
| MC001 | El daemon lee una clave de `capture_config` que `load_config_from_db()` no carga (cae al default en silencio) | CONFIRMED |
| MC002 | El SELECT de `capture_config` y los índices `r[i]` no cuadran | CONFIRMED |
| MC003 | `-reconnect_at_eof 1` (Teleceiba dejó de grabar) | CONFIRMED |
| MC004 | `Popen` sin grupo de procesos propio (ffmpeg huérfanos, load 96) | POTENTIAL |
| MC005 | `Popen` con `stderr=PIPE` (el pipe se llena y congela a ffmpeg) | POTENTIAL |
| MC006 | `execute()` dentro de un bucle (principio 5: batch, no loops por stream) | POTENTIAL |
| MC007 | `--hls-live-restart` sin condición por estación (canal_10 a 4x) | CONFIRMED |
| MC000 | Archivo `.py` que no parsea | POTENTIAL |

CONFIRMED significa que el patrón está en el código y ya causó un incidente; POTENTIAL, que hay
que revisarlo a mano. Ninguna regla dice que haya un bug en producción: para eso hay que
comparar con lo desplegado (`git status` en el server).

Un hallazgo revisado y aceptado se suprime en su línea, con la razón:

```python
subprocess.Popen(cmd)  # auditoria: ok MC004 proceso corto, se espera con wait()
```

Queda en el reporte como FALSE_POSITIVE, no desaparece.

Para correrlo local:

```bash
python auditoria/reglas_mediacap.py
python -m unittest tests.test_reglas_mediacap -v
```

Regla nueva: una función `regla_x(arbol, archivo)` en `REGLAS_PY`, con una prueba que reproduzca
el patrón del incidente y otra con la versión corregida.

## Recolector (mediaCAP → mediaAPP)

`recolector/` genera en mediaCAP un inventario de solo lectura de lo desplegado (git, hashes,
units, cron, versiones, disco) que el auditor lee por SSH sin poder ejecutar nada. Diseño,
garantías, instalación y rollback en [`recolector/INSTALAR.md`](recolector/INSTALAR.md).
