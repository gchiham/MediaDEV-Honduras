# Rollback Recorder Hardening

Instrucciones para volver al comportamiento anterior si el hardening del recorder causa problemas.

Objetivo del rollback: restaurar captura y uploads como estaban antes, sin borrar evidencia local ni objetos de S3.

## Cuándo usarlo

Usar este rollback si después del despliegue aparece cualquiera de estos síntomas:

- `stream-daemon` no arranca o queda reiniciando.
- `video-segment-uploader` no arranca o acumula backlog.
- Los streams siguen OK, pero dejan de aparecer MP3 horarios en S3.
- El CPU sube de forma sostenida y afecta captura.
- La tabla `recording_coverage` empieza a bloquear o ralentizar los servicios.

## Archivos tocados por el hardening

```text
daemon/stream_daemon.py
scripts/video_segment_uploader.py
deploy.sh
live_mediaDEV.md
migrations/20260616_recorder_hardening.sql
```

## Rollback rapido en produccion

Ejecutar en mediaCAP:

```bash
cd /opt/media-ai

# Guardar diagnostico antes de tocar nada
date
git status --short
systemctl status stream-daemon --no-pager
systemctl status video-segment-uploader --no-pager
supervisorctl status

# Volver al commit anterior estable
git fetch origin main
git reset --hard HEAD~1

# Reiniciar solo los servicios afectados
systemctl restart stream-daemon
systemctl restart video-segment-uploader

# Verificar
sleep 20
systemctl is-active stream-daemon video-segment-uploader
supervisorctl status
journalctl -u stream-daemon -n 80 --no-pager
journalctl -u video-segment-uploader -n 80 --no-pager
```

Si el cambio fue desplegado por `git pull` desde `main` y ya hay commits posteriores, no usar `HEAD~1` a ciegas. En ese caso usar el SHA estable anterior:

```bash
cd /opt/media-ai
git log --oneline -10
git reset --hard <SHA_ESTABLE_ANTERIOR>
systemctl restart stream-daemon video-segment-uploader
```

## Rollback sin perder la tabla nueva

Recomendado. No hace falta borrar `recording_coverage` para volver al codigo anterior. El codigo anterior simplemente no usa esa tabla.

Ventajas:

- No se pierde auditoria ya escrita.
- No se toca DB en emergencia.
- El rollback es reversible.

Verificar que los servicios volvieron:

```bash
systemctl is-active stream-daemon video-segment-uploader
supervisorctl status
```

## Rollback completo de DB

Usar solo si la tabla nueva causa problemas operativos claros o si se quiere limpiar totalmente el cambio.

Primero confirmar que los servicios ya estan usando codigo anterior:

```bash
cd /opt/media-ai
git status --short
systemctl is-active stream-daemon video-segment-uploader
```

Luego ejecutar en PostgreSQL:

```sql
DROP TABLE IF EXISTS recording_coverage;
```

Comando ejemplo desde mediaCAP:

```bash
PGPASSWORD="$PG_PASS" psql \
  -h "$PG_HOST" \
  -p "${PG_PORT:-25060}" \
  -U "$PG_USER" \
  -d "$PG_DB" \
  -c "DROP TABLE IF EXISTS recording_coverage;"
```

No borrar `s3_scan_log`, `fingerprint_detections`, objetos de S3 ni directorios de grabaciones.

## Si el problema es solo el uploader TV

Si radio esta funcionando y solo falla TV/video:

```bash
cd /opt/media-ai
git checkout HEAD~1 -- scripts/video_segment_uploader.py
systemctl restart video-segment-uploader
journalctl -u video-segment-uploader -n 100 --no-pager
```

Esto no reinicia streams ffmpeg y reduce impacto sobre captura.

## Si el problema es solo MP3 horario/radio

Si `video-segment-uploader` esta bien y solo falla `stream-daemon`:

```bash
cd /opt/media-ai
git checkout HEAD~1 -- daemon/stream_daemon.py
systemctl restart stream-daemon
journalctl -u stream-daemon -n 100 --no-pager
```

## Archivos locales a revisar despues del rollback

No borrar automaticamente. Revisar primero:

```bash
du -sh /var/www/streams/
find /var/www/streams -path '*/recordings/*.mp3' -mtime -1 -ls
find /var/www/streams/_tv_audio -type f 2>/dev/null | head
find /var/www/streams/_invalid -type f 2>/dev/null | head
```

Si hay MP3 locales recientes no subidos, conservarlos. Se pueden subir manualmente o esperar a una version corregida.

## Verificacion post-rollback

```bash
# Estado de streams
supervisorctl status

# Salud servicios
systemctl is-active stream-daemon video-segment-uploader

# Logs recientes
journalctl -u stream-daemon -n 120 --no-pager
journalctl -u video-segment-uploader -n 120 --no-pager

# Backlog local
find /var/www/streams -path '*/recordings/*.mp3' -mtime -1 | wc -l
find /var/www/streams -name 'seg_*.ts' | wc -l
```

Resultado esperado:

- Streams en `RUNNING`.
- `stream-daemon` activo.
- `video-segment-uploader` activo.
- Nuevos MP3 horarios aparecen en S3.
- El backlog local no crece sin control.

## Notas importantes

- No usar `rm -rf /var/www/streams/*` como parte del rollback.
- No borrar `_tv_audio` ni `_invalid` hasta confirmar que no contienen evidencia util.
- No reiniciar `supervisorctl restart all` salvo que el problema sea `stream_run.sh` o los procesos ffmpeg.
- Si se agregaron nuevos canales despues del commit estable anterior, confirmar que siguen en `stations.json` y en supervisord despues del rollback.

