#!/usr/bin/env python3
import os
from pathlib import Path
from datetime import datetime, timezone

import boto3
import subprocess
import tempfile


def load_env(path: str) -> None:
    for raw in Path(path).read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ[key] = value.strip().strip('"').strip("'")


def main() -> None:
    load_env("/etc/mediadev-s3.env")
    bucket = os.environ.get("S3_BUCKET", "mediadev-recordings")
    region = os.environ.get("S3_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)
    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    for stream in ("hch_tv", "teleceiba", "canal_11"):
        prefix = f"video_segments/{stream}/{today}/"
        objs = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            objs.extend(page.get("Contents", []))
        objs = sorted(objs, key=lambda o: o["LastModified"])
        print(f"=== {stream} ===")
        print(f"prefix={prefix}")
        print(f"objects_today={len(objs)}")
        for obj in objs[-5:]:
            print(f"{obj['LastModified'].isoformat()} {obj['Size']} {obj['Key']}")
        if objs:
            latest = objs[-1]
            with tempfile.NamedTemporaryFile(suffix=".ts") as tmp:
                s3.download_file(bucket, latest["Key"], tmp.name)
                video = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name,width,height",
                        "-of", "csv=p=0", tmp.name,
                    ],
                    capture_output=True, text=True,
                )
                audio = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=codec_name",
                        "-of", "csv=p=0", tmp.name,
                    ],
                    capture_output=True, text=True,
                )
                print(f"latest_download={latest['Key']}")
                print(f"ffprobe_video={video.stdout.strip() or video.stderr.strip()}")
                print(f"ffprobe_audio={audio.stdout.strip() or audio.stderr.strip()}")


if __name__ == "__main__":
    main()
