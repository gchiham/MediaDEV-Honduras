-- Rol de SOLO LECTURA para el vigía (fase 1). Lo corre un administrador (doadmin) en destroyer_db.
--
-- HECHO el 11-oct-2026 sin doadmin (no está en los nodos), por una vía equivalente:
--   1. usuario vigia_ro creado con la API de DigitalOcean (POST /v2/databases/<id>/users);
--   2. GRANT SELECT de abajo, ejecutado como destroyer (dueño de las tres tablas);
--   3. los ALTER ROLE ... SET, ejecutados por el propio vigia_ro (un rol puede fijar los suyos).
--   Sin CONNECTION LIMIT (requiere CREATEROLE). Verificado: solo lectura, solo SELECT en 3 tablas.
--   Registro: CHANGES.log de mediaAPP. Rollback: borrar el usuario en el panel/API de DO.
-- La contraseña NO va en este archivo: generarla en el momento y guardarla solo en
-- /etc/mediadev-agent.env de mediaAPP (DATABASE_URL_RO). Revisar antes de ejecutar.
--
--   psql "<DSN de doadmin a destroyer_db>" -v pw="'$(openssl rand -base64 24)'" -f rol_db_vigia.sql
--
-- El cluster aloja otras bases de otros proyectos. Ojo: PostgreSQL da CONNECT a PUBLIC por
-- defecto en cada base, así que vigia_ro podría CONECTARSE a otra base si su dueño no revocó ese
-- default; no tendría permisos sobre sus tablas salvo que esa base los dé a PUBLIC. Cerrarlo del
-- todo (REVOKE CONNECT ... FROM PUBLIC en las otras bases) toca otros proyectos: queda fuera.

CREATE ROLE vigia_ro LOGIN PASSWORD :pw CONNECTION LIMIT 3;
ALTER ROLE vigia_ro SET default_transaction_read_only = on;
ALTER ROLE vigia_ro SET statement_timeout = '5s';
ALTER ROLE vigia_ro SET idle_in_transaction_session_timeout = '30s';

REVOKE ALL ON DATABASE destroyer_db FROM vigia_ro;
GRANT CONNECT ON DATABASE destroyer_db TO vigia_ro;
GRANT USAGE ON SCHEMA public TO vigia_ro;

-- Fase 1: solo lo que lee vigia.py (estaciones habilitadas y estado del circuit breaker).
GRANT SELECT ON public.capture_config, public.media_sources, public.mediadev_stream_status TO vigia_ro;

-- Fase 2 (agente con IA; NO ejecutar hasta aprobarla):
-- GRANT SELECT ON public.mediadev_events, public.recording_coverage, public.s3_scan_log,
--                 public.gateways TO vigia_ro;

-- Verificación (como vigia_ro):
--   SHOW default_transaction_read_only;                                   -- on
--   SELECT has_table_privilege('public.capture_config', 'INSERT');        -- f
--   SELECT has_table_privilege('public.mediadev_events', 'SELECT');       -- f (fase 1)
