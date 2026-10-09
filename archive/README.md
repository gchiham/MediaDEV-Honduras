# archive/

Código y documentos que **ya no corren en mediaCAP**. Se conservan como referencia histórica;
no forman parte del sistema actual. Ver `MAPA_MEDIACAP.md` en la raíz.

- `dashboard/`: dashboard Flask v4, eliminado de producción el 14-jun-2026.
- `deploy.sh`: deploy por git pull (no se usa; hoy el deploy es manual por SSH + CHANGES.log).
- `scripts/release.sh`: releases del Destroyer viejo en DO (hoy corre en AWS, repo destroyer).
- `scripts/deploy_peer_b.sh`, `backup_healthcheck.py`: despliegue de un nodo B que no existe.
- `scripts/check_tv_*`, `backfill_*`, `apply_*_migration.py`: scripts de una sola vez (jun-2026).
- `docs/`: guías de junio-2026 que describen la arquitectura anterior (supervisor, dashboard, API REST).
