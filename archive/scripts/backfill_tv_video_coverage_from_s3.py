#!/usr/bin/env python3
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import psycopg2


TV_STREAMS = ("hch_tv", "teleceiba", "canal_11")
SEGMENT_SECONDS = 4


def load_env(path: str) -> None:
    for raw in Path(path).read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ[key] = value.strip().strip('"').strip("'")


def parse_epoch_from_key(key: str) -> int | None:
    name = key.rsplit("/", 1)[-1].removesuffix(".ts")
    first = name.split("_", 1)[0]
    try:
        return int(first)
    except ValueError:
        return None


def main() -> None:
    days = int(os.environ.get("BACKFILL_DAYS", "7"))
    load_env("/etc/mediadev-s3.env")
    load_env("/etc/mediadev-db.env")

    bucket = os.environ.get("S3_BUCKET", "mediadev-recordings")
    region = os.environ.get("S3_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)

    now = datetime.now(timezone.utc)
    dates = [(now - timedelta(days=i)).strftime("%Y/%m/%d") for i in range(days)]
    grouped: dict[tuple[str, datetime], dict[str, int]] = defaultdict(lambda: {"segs": 0, "bytes": 0})

    for stream in TV_STREAMS:
        for date_part in dates:
            prefix = f"video_segments/{stream}/{date_part}/"
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    epoch = parse_epoch_from_key(obj["Key"])
                    if epoch is None:
                        continue
                    hour = datetime.fromtimestamp(epoch, tz=timezone.utc).replace(
                        minute=0, second=0, microsecond=0
                    )
                    rec = grouped[(stream, hour)]
                    rec["segs"] += 1
                    rec["bytes"] += int(obj.get("Size") or 0)

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
        for (stream, hour), rec in sorted(grouped.items()):
            cur.execute(
                """
                INSERT INTO mediadev_video_coverage (stream, hour_utc, segs, bytes, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (stream, hour_utc) DO UPDATE SET
                  segs = GREATEST(mediadev_video_coverage.segs, EXCLUDED.segs),
                  bytes = GREATEST(mediadev_video_coverage.bytes, EXCLUDED.bytes),
                  updated_at = NOW()
                """,
                (stream, hour, rec["segs"], rec["bytes"]),
            )
    conn.close()
    print(f"backfilled_hours={len(grouped)}")


if __name__ == "__main__":
    main()
