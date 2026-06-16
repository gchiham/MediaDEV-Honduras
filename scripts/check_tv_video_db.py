#!/usr/bin/env python3
import os
from pathlib import Path

import psycopg2


def load_env(path: str) -> None:
    for raw in Path(path).read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ[key] = value.strip().strip('"').strip("'")


def main() -> None:
    load_env("/etc/mediadev-db.env")
    conn = psycopg2.connect(
        host=os.environ["PG_HOST"],
        port=int(os.environ.get("PG_PORT", "25060")),
        dbname=os.environ["PG_DB"],
        user=os.environ["PG_USER"],
        password=os.environ["PG_PASS"],
        sslmode="require",
        connect_timeout=10,
    )
    with conn, conn.cursor() as cur:
        for table in ("recording_coverage", "mediadev_video_coverage"):
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            print(table, cur.fetchone()[0])

        cur.execute(
            """
            SELECT stream_id, media_type, status, count(*), max(updated_at)
            FROM recording_coverage
            WHERE stream_id IN ('hch_tv','teleceiba','canal_11')
            GROUP BY 1,2,3
            ORDER BY 1,2,3
            """
        )
        print("recording_coverage")
        for row in cur.fetchall():
            print(row)

        cur.execute(
            """
            SELECT stream, count(*), max(hour_utc), sum(segs), sum(bytes)
            FROM mediadev_video_coverage
            WHERE stream IN ('hch_tv','teleceiba','canal_11')
            GROUP BY 1
            ORDER BY 1
            """
        )
        print("mediadev_video_coverage")
        for row in cur.fetchall():
            print(row)
    conn.close()


if __name__ == "__main__":
    main()
