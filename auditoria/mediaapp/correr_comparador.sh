#!/bin/bash
# mediaAPP: trae el inventario de mediaCAP, actualiza los clones y corre el comparador.
# Corre como el usuario `auditor` desde mediacap-auditor-comparador.timer. Nunca escribe en mediaCAP:
# la llave solo permite recibir el inventario (ForceCommand cat en mediaCAP).
set -uo pipefail
BASE=/var/lib/mediacap-auditor
CODIGO=/opt/mediacap-auditor
REPOS=$BASE/repos
SALIDA=$BASE/reportes
RETENCION_DIAS=30
ts=$(date -u +%Y%m%dT%H%MZ)
umask 027
mkdir -p "$SALIDA"

for r in MediaDEV-Honduras destroyer mediadev-infra; do
  timeout 60 git -C "$REPOS/$r" fetch -q --prune origin || { echo "fetch falló: $r" >&2; exit 2; }
done

inv="$SALIDA/inventario-$ts.json"
timeout 60 ssh -i "$BASE/.ssh/mediacap_auditor" -o BatchMode=yes -o ConnectTimeout=10 \
  -o UserKnownHostsFile="$BASE/.ssh/known_hosts" -o StrictHostKeyChecking=yes \
  auditor@10.136.0.7 > "$inv" 2>/dev/null
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$inv" \
  || { echo "inventario inválido o vacío" >&2; rm -f "$inv"; exit 2; }

python3 -I "$CODIGO/auditoria/comparador.py" --inventario "$inv" \
  --captura "$REPOS/MediaDEV-Honduras" --destroyer "$REPOS/destroyer" --infra "$REPOS/mediadev-infra" \
  --json "$SALIDA/drift-$ts.json" --md "$SALIDA/drift-$ts.md" > /dev/null
rc=$?
[ "$rc" -gt 1 ] && { echo "comparador falló (rc=$rc)" >&2; exit "$rc"; }
ln -sfn "drift-$ts.md" "$SALIDA/ultimo.md"
ln -sfn "drift-$ts.json" "$SALIDA/ultimo.json"
find "$SALIDA" -maxdepth 1 -type f -mtime +"$RETENCION_DIAS" -delete
python3 -c "
import json,sys,collections
d=json.load(open(sys.argv[1])); c=collections.Counter(h['estado'] for h in d['hallazgos'])
print('drift', sys.argv[2], dict(sorted(c.items())))" "$SALIDA/drift-$ts.json" "$ts"
exit 0     # rc=1 (hay CONFIRMED) es un resultado, no una falla del servicio
