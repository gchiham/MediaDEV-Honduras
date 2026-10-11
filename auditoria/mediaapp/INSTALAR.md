# Comparador en mediaAPP

Cada 6 h (00/6:50 UTC, 15 min después del recolector de mediaCAP) el usuario `auditor` de mediaAPP:

1. hace `git fetch` de sus 3 clones (`MediaDEV-Honduras` público por HTTPS; `destroyer` y
   `mediadev-infra` con **deploy keys de solo lectura**, una por repo);
2. pide el inventario a mediaCAP (`ssh auditor@10.136.0.7`: solo puede recibir ese JSON);
3. corre `auditoria/comparador.py` y deja `reportes/drift-<UTC>.{md,json}` y `reportes/ultimo.md`
   (30 días de retención).

```
/var/lib/mediacap-auditor/            auditor:auditor 750 (home del usuario, sin shell)
  .ssh/mediacap_auditor               llave a mediaCAP (antes en /root/.ssh, movida)
  .ssh/deploy_destroyer, deploy_infra deploy keys read-only (GitHub: "mediaAPP auditor mediaCAP")
  .ssh/known_hosts                    mediaCAP y github.com, huellas verificadas al instalar
  repos/  reportes/
/opt/mediacap-auditor/auditoria/      código (root:root, el auditor no puede editarlo)
/etc/systemd/system/mediacap-auditor-comparador.{service,timer}
```

El servicio corre como `auditor` con `ProtectSystem=strict` (solo escribe en
`/var/lib/mediacap-auditor`), `NoNewPrivileges`, `Nice=10`, `CPUQuota=50%`, `MemoryMax=300M`.
No comparte nada con media-app ni con el vigía. `rc=1` del comparador (hay CONFIRMED) no es falla.

```bash
systemctl start mediacap-auditor-comparador.service        # corrida manual
cat /var/lib/mediacap-auditor/reportes/ultimo.md           # último reporte
journalctl -u mediacap-auditor-comparador -n 20
```

## Actualizar el código

```bash
git -C <clon> archive <commit> auditoria | tar -x -C /opt/mediacap-auditor   # como root
```

## Rollback

```bash
systemctl disable --now mediacap-auditor-comparador.timer
rm /etc/systemd/system/mediacap-auditor-comparador.{service,timer}; systemctl daemon-reload
rm -rf /opt/mediacap-auditor; userdel -r auditor          # borra también /var/lib/mediacap-auditor
gh repo deploy-key delete <id> --repo gchiham/destroyer; gh repo deploy-key delete <id> --repo gchiham/mediadev-infra
```

(Sin la llave de mediaCAP el recolector sigue funcionando; para retirarlo, ver `recolector/INSTALAR.md`.)
