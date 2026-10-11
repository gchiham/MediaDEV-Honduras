# Recolector del auditor — instalación en mediaCAP

El auditor de código corre en **mediaAPP** y necesita saber qué hay desplegado en mediaCAP
(git, hashes, units, cron, versiones, disco). Este recolector se lo da **sin que el auditor
pueda ejecutar nada en mediaCAP**:

```
mediaCAP (root, timer cada 6 h)                         mediaAPP
 mediacap-auditor-inventario  ──escribe──►  inventario.json  ◄──ssh auditor@──  auditor
 ProtectSystem=strict: todo en solo         root:auditor 0640   ForceCommand = cat de ese archivo
 lectura salvo /var/lib/mediacap-auditor                         (ignora lo que pida el cliente)
```

| Garantía | Cómo se impone (técnico, no por prompt) |
|---|---|
| El recolector no modifica el servidor | `ProtectSystem=strict` + un solo `ReadWritePaths`; `git --no-optional-locks` |
| No compite con la captura | `Nice=19`, `IOSchedulingClass=idle`, `CPUQuota=20%`, `MemoryMax=200M`; corre a las :35 cada 6 h, lejos del cierre horario (1.4 s de CPU) |
| El auditor no ejecuta nada | `ForceCommand /bin/cat …` en sshd **y** `command=` en la llave; también anula sftp/scp |
| Sin túneles ni shell | `restrict` en la llave + `PermitTTY/AllowTcpForwarding/... no` en sshd |
| Solo desde mediaAPP | `from="10.136.0.6"` en la llave |
| Sin secretos | `/etc/*.env` y `/etc/mediadev/` solo metadatos; units/cron redactados; el verificador cruza los valores reales |
| No afecta al vigía | Usuario, llave, bloque `Match` y archivos propios; nada del vigía se lee ni se edita |

## Instalación (como root en mediaCAP, con otra sesión de root abierta)

Antes: leer `/opt/media-ai/CHANGES.log` y escribir la entrada PLAN.

```bash
# 1. Usuario sin shell útil, sin home, sin contraseña.
useradd --system --no-create-home --home-dir /nonexistent --shell /bin/sh auditor
passwd -l auditor

# 2. Recolector y directorio de salida (ReadWritePaths debe existir o el servicio falla con 226).
install -o root -g root -m 755 auditoria/recolector/mediacap_inventario.py /usr/local/bin/mediacap-auditor-inventario
install -d -o root -g auditor -m 750 /var/lib/mediacap-auditor

# 3. Servicio + timer; primera corrida.
install -o root -g root -m 644 auditoria/recolector/instalar/mediacap-auditor-inventario.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl start mediacap-auditor-inventario.service
systemctl enable --now mediacap-auditor-inventario.timer

# 4. Llave: generarla EN mediaAPP (ssh-keygen -t ed25519 -f /root/.ssh/mediacap_auditor -N "" -C mediacap-auditor)
#    y poner solo la pública con el prefijo de authorized_keys.ejemplo:
install -d -o root -g root -m 755 /etc/ssh/authorized_keys
install -o root -g root -m 644 /dev/null /etc/ssh/authorized_keys/auditor   # y pegar la línea

# 5. sshd: agregar sshd_auditor.conf AL FINAL de /etc/ssh/sshd_config, validar y recargar.
sshd -t && sshd -T -C user=auditor,host=x,addr=10.136.0.6 | grep -iE "forcecommand|permittty|forwarding"
systemctl reload ssh

# 6. Verificar (solo lectura). Tiene que decir TODO OK.
bash auditoria/recolector/instalar/verificar_mediacap.sh auditoria/recolector/instalar/permisos_mediacap.txt
```

Desde mediaAPP, las pruebas negativas (todo pedido devuelve solo el inventario; sin TTY, túnel ni sftp):

```bash
bash auditoria/recolector/instalar/probar_desde_mediaapp.sh /root/.ssh/mediacap_auditor
```

Después: entrada DONE en `CHANGES.log`.

## Rollback (deja mediaCAP como antes)

```bash
systemctl disable --now mediacap-auditor-inventario.timer
rm /etc/systemd/system/mediacap-auditor-inventario.{service,timer} /usr/local/bin/mediacap-auditor-inventario
rm -rf /var/lib/mediacap-auditor /etc/ssh/authorized_keys/auditor
# quitar el bloque "Match User auditor" de /etc/ssh/sshd_config; sshd -t && systemctl reload ssh
userdel auditor
systemctl daemon-reload
```

## Qué trae el inventario

`sistema`, `versiones` (python3, ffmpeg, ffprobe, streamlink, git), `git` de `/opt/media-ai`
(HEAD, rama, archivos modificados), `codigo_sin_git` (sha256 de `/opt/destroyer` sin venv),
`systemd` (estado + texto redactado y hash de cada unit y drop-in), `cron` (redactado),
`secretos_meta` (dueño/modo/tamaño, nunca contenido), `disco` (df, inodos, du de streams y logs),
`procesos` (conteo por comando, ffmpeg con PPID=1; sin argumentos) y `venvs` (paquetes por
nombre de `dist-info`). Cada sección falla sola y queda en `errores`.

Lo que **no** trae: grabaciones, segmentos, contenido de DB, argumentos de procesos (las URLs de
ffmpeg llevan credenciales) ni contenido de archivos de secretos.
