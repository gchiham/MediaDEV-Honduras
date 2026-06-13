"""
db.py — Conexión PostgreSQL de solo lectura para el MCP server.
Lee credenciales de /etc/mediadev-db.env (igual que el daemon y el dashboard).
"""
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import dotenv_values

def _load_pg_env() -> dict:
    """Carga credenciales desde /etc/mediadev-db.env o variables de entorno."""
    env_file = Path("/etc/mediadev-db.env")
    cfg = {}
    if env_file.exists():
        cfg = dotenv_values(env_file)
    # Variables de entorno tienen prioridad
    for key in ("PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASS"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg

_pg_cfg = _load_pg_env()

@contextmanager
def get_db():
    """Context manager que da una conexión PG de solo lectura."""
    conn = psycopg2.connect(
        host=_pg_cfg.get("PG_HOST", "localhost"),
        port=int(_pg_cfg.get("PG_PORT", "5432")),
        dbname=_pg_cfg.get("PG_DB", "destroyer_db"),
        user=_pg_cfg.get("PG_USER", "destroyer"),
        password=_pg_cfg.get("PG_PASS", ""),
        connect_timeout=8,
        sslmode="require",
        options="-c default_transaction_read_only=on",  # Garantía de solo lectura
    )
    try:
        yield conn
    finally:
        conn.close()

def qall(sql: str, params: tuple = ()) -> list[dict]:
    """Ejecuta una query y retorna todas las filas como lista de dicts."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

def qone(sql: str, params: tuple = ()):
    """Ejecuta una query y retorna la primera fila como dict, o None."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

def db_ping() -> dict:
    """Verifica conectividad y mide latencia."""
    import time
    t0 = time.monotonic()
    try:
        row = qone("SELECT 1 AS ok")
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return {"status": "ok", "latency_ms": latency_ms}
    except Exception as e:
        return {"status": "error", "error": str(e)}
