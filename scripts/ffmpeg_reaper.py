#!/usr/bin/env python3
"""Reaper de ffmpeg duplicados por stream (mediaCAP). Red de seguridad contra el bug de
stream_daemon que apila ffmpeg cuando el source flaky cuelga (incidente load-96 jun-16,
teleceiba jun-29). Cada stream de captura debe tener 1 ffmpeg escribiendo a
/var/www/streams/<stream>/. Si hay >1, deja el mas reciente y mata los viejos. NO toca el
grabador MP3 (otro path) ni nada fuera de /var/www/streams. Corre cada 5 min por systemd timer."""
import re, subprocess, collections, os, signal
out = subprocess.run(['ps','-eo','pid,etimes,args'], capture_output=True, text=True).stdout
groups = collections.defaultdict(list)
for line in out.splitlines():
    if 'ffmpeg' not in line:
        continue
    m = re.search(r'/var/www/streams/([^/ ]+)/', line)
    if not m:
        continue
    parts = line.split(None, 2)
    try:
        pid, etimes = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        continue
    groups[m.group(1)].append((etimes, pid))
reaped = []
for stream, procs in groups.items():
    if len(procs) <= 1:
        continue
    procs.sort()  # asc por etimes: [0] = mas reciente
    for etimes, pid in procs[1:]:
        try:
            os.kill(pid, signal.SIGKILL); reaped.append((stream, pid, etimes))
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f'  no pude matar {stream} pid={pid}: {e}')
if reaped:
    for stream, pid, et in reaped:
        print(f'REAPED {stream} pid={pid} (edad {et}s)')
    print(f'total reaped={len(reaped)} streams={sorted(set(s for s,_,_ in reaped))}')
else:
    print(f'ok: {len(groups)} streams, 1 ffmpeg c/u, nada que reapear')
