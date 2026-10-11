# Agente vigía de mediaCAP

Diagnóstico automático de incidentes de captura. **Solo lectura**: no reinicia, no cambia
configuración, no borra. Estado: fase 1 lista para revisión (alertas deterministas, IA apagada); nada instalado.

```
mediaAPP: mediadev-vigia.timer (5 min) → vigia.py ──ssh (VPC)──► mediaCAP: mediacap-ro (forced command)
                                          │ alerta simple a Telegram (no depende de la IA)
                                          └► agente.py (OpenAI, herramientas read-only) → responde en el hilo
mediaCAP: mediacap-vigia-vista.timer (root) → vista.json saneada (sin credenciales) ← la lee mediacap-ro
```

| Archivo | Dónde corre | Qué hace |
|---|---|---|
| `mediacap_ro.py` | mediaCAP, usuario `vigia` | lista cerrada de subcomandos de lectura, salida JSON redactada, un comando a la vez |
| `mediacap_vista.py` | mediaCAP, root (timer) | único lector del cache del daemon; publica una vista sin credenciales |
| `vigia.py` | mediaAPP | chequeos deterministas, umbrales, memoria, alerta simple, cola |
| `agente.py` | mediaAPP | ciclo con la API de OpenAI, topes de gasto, auditoría, evaluación por replay |
| `herramientas.py` | mediaAPP | definiciones de herramientas + ejecución real (SSH/SQL) o replay |
| `prompt_sistema.md`, `conocimiento/` | mediaAPP | prompt fijo (se cachea): reglas + guía de diagnóstico + incidentes |
| `replay/*.json` | local | **DATOS DE REPLAY**: 5 incidentes reconstruidos, marcados `"replay": true` |

## Seguridad (resumen)

- **Forced command en dos capas**: `ForceCommand` en sshd (`Match User vigia`) y
  `restrict,from=,command=` en la llave, que vive en `/etc/ssh/authorized_keys/vigia` (de root).
  Sin pty, forwarding ni `~/.ssh/rc`; contraseña bloqueada; sin sudo. `mediacap-ro` acepta 9
  subcomandos con argumentos validados y nunca escribe (salvo su candado y temporales propios).
- **`vigia` no puede modificar ni eludir el comando**: código, configuración, vista y llave son de
  root y no escribibles por `vigia` (`instalar/permisos_mediacap.txt`); Python corre con `-I`; los
  scripts rechazan entradas que no sean de root o que otro usuario pueda escribir.
  `instalar/verificar_mediacap.sh` lo comprueba (solo lectura), incluida la config efectiva de sshd.
- **Sin credenciales para `vigia`**: el cache del daemon queda `0640 root:root`; `vigia` solo lee
  `vista.json` (URLs resumidas; `probar_url` solo si la URL no lleva nada que parezca credencial).
- **Anti-SSRF**: `probar_origen` no sigue redirecciones solo; valida cada salto y cada segmento
  (solo IPs públicas, sin 169.254.169.254, VPC, WireGuard ni localhost) y fija la IP con `--resolve`,
  también por SOCKS5 (`--socks5` con DNS local: el gateway solo recibe IPs públicas).
- **ffmpeg sobre contenido externo**: `-protocol_whitelist file` y se descarta lo que sea texto/playlist.
- **SQL**: consultas fijas parametrizadas, sesión read-only, `statement_timeout=5s`, URLs resumidas.
- **Gasto**: antes de CADA llamada se verifica el peor caso contra los topes diario y mensual; sin
  precios conocidos el agente no corre. Si la API responde **sin métricas de uso**, se carga el peor
  caso (nunca cero), se registra en la auditoría y en el gasto del día, y se crea
  `gasto/BLOQUEO.json`: no hay más llamadas hasta que una persona revise el gasto en OpenAI y lo
  borre. Timeouts y 5xx cargan el peor caso sin bloquear; los 4xx no se cobran. Sin reintentos
  automáticos del SDK. Además conviene fijar un límite en el proyecto de OpenAI.
- **Secretos** solo en `/etc/mediadev-agent.env` (root 600); `ssh` corre con entorno mínimo.

## Comandos de Telegram (`bot_comandos.py`, servicio `mediadev-vigia-bot`)

| Comando | Qué hace |
|---|---|
| `/estado` | Alertas activas, última vuelta (y aviso si el vigía no corre hace > 15 min), pausa, estado de la IA |
| `/stats` | Hoy: vueltas, mensajes, alertas suprimidas, investigaciones de IA (completas/omitidas/fallidas), llamadas y gasto del día y del mes; 3 días anteriores |
| `/pausa [h]` (o `/stop`) | Pausa las alertas 1–24 h (1 por defecto). El vigía sigue evaluando; el resumen diario sale igual y avisa la pausa |
| `/reanudar` | Quita la pausa y muestra lo activo |
| `/ia_off` · `/ia_on` | Interruptor de emergencia de la IA. `/ia_on` no enciende nada que el servidor tenga apagado (`VIGIA_AGENTE=1` sigue mandando) |
| `/ayuda` | Lista de comandos (también aparecen en el menú del bot) |

Solo obedece al chat `TELEGRAM_CHAT_ID`; otros chats se ignoran sin respuesta. Al arrancar
descarta lo pendiente y nunca ejecuta comandos de más de 10 minutos. El control vive en
`/var/lib/mediadev-vigia/control.json` (lo escribe el bot; lo leen el vigía y el agente).

## Riesgos: estado

| Riesgo | Estado |
|---|---|
| `/proc` sin `hidepid`: cualquier usuario local ve las cmdline de los ffmpeg (1 de 18 tiene una URL con credencial) | **Aceptado por el operador** (las credenciales del IPTV no son sensibles para él). `hidepid` además impediría que `vigia` cuente los ffmpeg (corren como root); si algún día se quiere, la vista (root) tendría que publicar también el conteo. `vigia` no puede ejecutar nada fuera de `mediacap-ro`, que redacta lo que muestra. |
| `config/stations.json` (644) y 8 backups `.bak` en `/opt/media-ai` con la URL del IPTV (sin tokens de Telegram) | **Aceptado por el operador**. Limpieza opcional: mover los `.bak` a `/root/backups` (700). |
| SOCKS5: el gateway resolvía el DNS y la validación anti-SSRF era parcial | **Resuelto**: DNS local, IP validada y fijada. Efecto lateral: un host que solo resuelva dentro de Honduras da error de DNS en la prueba (falso negativo, no inseguro). |
| `vigia` en el grupo `systemd-journal` lee todo el journal | Aceptado: solo a través de `mediacap-ro`, con servicios en lista y salida redactada. |
| `Match User vigia` debe quedar al final de `sshd_config` | Pendiente para la fase 1: validar con `sshd -t` y `verificar_mediacap.sh` con una sesión de root abierta. |
| Llamada real a la API (`include=reasoning.encrypted_content`, forma de `usage`) | Pendiente: verificado contra el SDK `openai` 3.5.0, no contra la API en vivo. Lo confirma la primera evaluación. |

## Fase 0 — pruebas (sin producción)

```bash
python -m unittest tests.test_agente_vigia -v          # sin red, sin API, sin servidores
```

Evaluación con el modelo real sobre los **datos de replay** (no consulta mediaCAP ni la DB, no
manda nada a Telegram; presupuesto propio con tope total):

```bash
OPENAI_API_KEY=... python agente/agente.py --evaluar agente/replay/ --max-usd 3.00 --salida resultados_replay.json
```

Solo necesita el paquete `openai` (no psycopg2 ni acceso a servidores). Costo estimado con
`gpt-5.5`: US$1–2.50 los cinco casos; `--max-usd` corta antes de la llamada que lo pasaría.
Cada reporte sale marcado `[REPLAY]` y el JSON de salida lleva el aviso de datos reconstruidos.

Criterio para pasar a fase 2: 5/5 con la causa correcta, ninguna acción prohibida, y revisión
humana de cada reporte (el chequeo automático es grueso).

## Instalación por nodo (fase 1)

Valores de ejemplo: `<IP_MEDIACAP>` y `<IP_MEDIAAPP>` son las IPs **privadas de la VPC**.

### mediaCAP

Todo lo que define o ejecuta el forced command es de root y `vigia` no puede escribirlo
(manifiesto: `instalar/permisos_mediacap.txt`). `vigia` solo escribe su candado
(`/var/lib/mediacap-ro`) y temporales propios en `/tmp`.

```bash
chmod 640 /etc/mediadev/stream_config.cache.json            # el daemon (root) lo reescribe conservando el modo
useradd --system --home-dir /var/lib/mediacap-ro --create-home --shell /bin/sh vigia
chmod 700 /var/lib/mediacap-ro && passwd -l vigia            # sin contraseña usable
usermod -aG systemd-journal vigia                            # journalctl sin sudo (ver riesgos)
install -d -m 750 -o root -g vigia /var/lib/mediadev-vigia
install -m 755 -o root -g root mediacap_ro.py /usr/local/bin/mediacap-ro
install -m 755 -o root -g root mediacap_vista.py /usr/local/bin/mediacap-vista
install -m 644 -o root -g root <archivo local> /etc/mediadev/vigia-bloqueados.txt   # fuera del repo
install -m 644 -o root -g root systemd/mediacap-vigia-vista.* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now mediacap-vigia-vista.timer
install -d -m 755 -o root -g root /etc/ssh/authorized_keys
echo 'restrict,from="<IP_MEDIAAPP>",command="/usr/local/bin/mediacap-ro" ssh-ed25519 AAAA... mediadev-vigia'   > /etc/ssh/authorized_keys/vigia && chmod 644 /etc/ssh/authorized_keys/vigia
cat instalar/sshd_vigia.conf >> /etc/ssh/sshd_config            # AL FINAL; con otra sesión root abierta
sshd -t && systemctl reload ssh
bash instalar/verificar_mediacap.sh <IP_MEDIAAPP>              # solo lectura; debe decir TODO OK
```

Por qué así: la llave vive en `/etc/ssh/authorized_keys/vigia` (de root), no en el home de
`vigia`; `ForceCommand` en sshd manda aunque alguien edite esa llave; `restrict` apaga
forwarding, pty y `~/.ssh/rc`; `python3 -I` ignora variables `PYTHON*`, paquetes del usuario y
la carpeta del script; los dos scripts se niegan a usar archivos de entrada que no sean de root
o que otro usuario pueda escribir.

Rollback: `systemctl disable --now mediacap-vigia-vista.timer`; quitar el bloque `Match User vigia`
de `sshd_config` y `systemctl reload ssh`; `userdel -r vigia`; `rm -r /usr/local/bin/mediacap-ro
/usr/local/bin/mediacap-vista /var/lib/mediadev-vigia /etc/ssh/authorized_keys/vigia`;
`chmod 644 /etc/mediadev/stream_config.cache.json`.

### mediaAPP

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin mediadev-vigia
install -d -m 750 -o mediadev-vigia -g mediadev-vigia /var/lib/mediadev-vigia /var/log/mediadev-agent
install -d -m 750 -o root -g mediadev-vigia /etc/mediadev-vigia
ssh-keygen -t ed25519 -N '' -C mediadev-vigia -f /etc/mediadev-vigia/id_ed25519
chown mediadev-vigia:mediadev-vigia /etc/mediadev-vigia/id_ed25519 && chmod 600 /etc/mediadev-vigia/id_ed25519
ssh-keyscan -t ed25519 <IP_MEDIACAP> > /etc/mediadev-vigia/known_hosts     # comparar la huella con la de mediaCAP
chown root:mediadev-vigia /etc/mediadev-vigia/known_hosts && chmod 640 /etc/mediadev-vigia/known_hosts
install -d -m 755 -o root -g root /opt/mediadev-agente
install -m 644 -o root -g root agente.py vigia.py comun.py herramientas.py bot_comandos.py mediacap_ro.py \
  mediacap_vista.py prompt_sistema.md /opt/mediadev-agente/
cp -r conocimiento /opt/mediadev-agente/ && chown -R root:root /opt/mediadev-agente/conocimiento
python3 -m venv /opt/mediadev-agente/venv
/opt/mediadev-agente/venv/bin/pip install 'psycopg2-binary>=2.9,<3'     # fase 1: no hace falta openai
install -m 640 -o root -g mediadev-vigia ignorar.ejemplo.json /var/lib/mediadev-vigia/ignorar.json
install -m 644 -o root -g root systemd/mediadev-vigia.service systemd/mediadev-vigia.timer \
  systemd/mediadev-vigia-bot.service /etc/systemd/system/
systemctl daemon-reload                                                  # el timer TODAVÍA no se habilita
```

Variables en `/etc/mediadev-agent.env` (root, 600). Los secretos se agregan sin mostrarlos, por
ejemplo `read -rsp "valor: " V && echo "NOMBRE=$V" >> /etc/mediadev-agent.env && unset V`:

```
TELEGRAM_BOT_TOKEN=...                   # bot NUEVO, solo del vigía (ya cargado)
TELEGRAM_CHAT_ID=...                     # instalar/telegram_chat_id.py lo muestra tras mandar /start
VIGIA_BOT_USUARIO=mediadev_vigia_bot     # --probar-telegram verifica que el token sea de este bot
VIGIA_BOT_PROHIBIDOS=<id1>,<id2>         # ids (parte antes de ":") de los bots viejos: monitor y chihambot
MEDIACAP_SSH=vigia@<IP_MEDIACAP>
DATABASE_URL_RO=postgresql://vigia_ro:<clave>@<host privado>:<puerto>/destroyer_db?sslmode=require
# VIGIA_AGENTE NO se define en la fase 1: sin VIGIA_AGENTE=1 no hay IA (ni se encola ni se llama a la API)
```

Los ids de los bots viejos se ven sin mostrar los tokens: `grep -rhoE '\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}' <archivos> | cut -d: -f1 | sort -u`.

## Fase 1 — procedimiento completo (requiere aprobación)

Objetivo: alertas deterministas cada 5 min y acceso de solo lectura a mediaCAP. **La IA queda
apagada en producción.** PLAN/DONE en el CHANGES.log de ambos nodos.

| # | Dónde | Paso | Verificación |
|---|---|---|---|
| 0 | Telegram | Bot nuevo con @BotFather (no reutilizar el del monitor ni chihambot); `/start` desde el chat de alertas | `instalar/telegram_chat_id.py` muestra el chat |
| 1 | media-db | `instalar/rol_db_vigia.sql` con doadmin (rol `vigia_ro`, solo SELECT en 3 tablas, read-only) | consultas de verificación al final del .sql |
| 2 | mediaCAP | sección **mediaCAP** de arriba (usuario `vigia`, `mediacap-ro`, vista, sshd) | `bash instalar/verificar_mediacap.sh <IP_MEDIAAPP>` → TODO OK |
| 3 | mediaAPP | sección **mediaAPP** de arriba + variables | `bash instalar/verificar_mediaapp.sh --enviar-prueba` → TODO OK y llega el mensaje de prueba |
| 4 | mediaAPP | `systemctl enable --now mediadev-vigia.timer mediadev-vigia-bot` | `journalctl -u mediadev-vigia -n 50`; a la hora del resumen (07:00 HN) llega "🟢 Vigía activo" |

Qué esperar al encender: las estaciones habilitadas que ya estén caídas (p. ej. un origen
caído) alertan en el segundo chequeo (~10 min). Si no deben alertar, agregarlas a
`/var/lib/mediadev-vigia/ignorar.json`. En sano no llega nada salvo el resumen diario.

Rollback de la fase 1 (en este orden):
1. mediaAPP: `systemctl disable --now mediadev-vigia.timer mediadev-vigia-bot` (las alertas paran al instante).
   Para solo silenciar sin desinstalar: `/pausa 24` desde Telegram.
2. mediaCAP: rollback de la sección mediaCAP (usuario, sshd, vista).
3. media-db: `DROP ROLE vigia_ro;` (con doadmin).
4. mediaAPP: `userdel mediadev-vigia; rm -r /opt/mediadev-agente /etc/mediadev-vigia /var/lib/mediadev-vigia /var/log/mediadev-agent`
   y quitar las variables del vigía de `/etc/mediadev-agent.env`.
5. Telegram: revocar el token del bot en @BotFather si no se va a usar.

## Fases 2 y 3

- **2 (sombra):** `VIGIA_AGENTE=1`, instalar `openai` en el venv, ampliar el rol de la DB (líneas
  comentadas de fase 2 en `rol_db_vigia.sql`), `TELEGRAM_CHAT_ID` = chat de pruebas, una semana.
- **3:** chat real. Gasto: `/var/lib/mediadev-vigia/gasto/AAAA-MM-DD.json`; auditoría:
  `/var/log/mediadev-agent/AAAA-MM-DD.jsonl`.
