#!/usr/bin/env python3
"""Move collector state and newly downloaded flight files to/from Cloudflare R2."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import mimetypes
import os
from pathlib import Path
import sqlite3

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def client_and_bucket():
    required = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
    )
    return client, os.environ["R2_BUCKET"]


def key(prefix: str, suffix: str) -> str:
    return f"{prefix.strip('/')}/{suffix.lstrip('/')}"


def download_optional(client, bucket: str, object_key: str, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download_file(bucket, object_key, str(destination))
        print(f"Restored {object_key}")
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            print(f"No stored {object_key}; starting a new archive")
            return False
        raise


def pull(client, bucket: str, prefix: str, root: Path) -> None:
    # Flight payloads stay in R2. The SQLite index is sufficient for resume.
    restored = download_optional(client, bucket, key(prefix, "state/index.sqlite3"), root / "index.sqlite3")
    download_optional(client, bucket, key(prefix, "state/flights.csv"), root / "flights.csv")
    if restored:
        db = sqlite3.connect(root / "index.sqlite3")
        try:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Restored SQLite index failed its integrity check")
            count = db.execute("SELECT COUNT(*) FROM flights WHERE status='complete'").fetchone()[0]
            print(f"Restored checkpoint with {count} complete flights")
        finally:
            db.close()


def upload_file(client, bucket: str, source: Path, object_key: str) -> None:
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    client.upload_file(str(source), bucket, object_key, ExtraArgs={"ContentType": content_type})
    print(f"Uploaded {object_key} ({source.stat().st_size} bytes)")


def push(client, bucket: str, prefix: str, root: Path) -> None:
    database = root / "index.sqlite3"
    if not database.is_file():
        raise RuntimeError("Collector index does not exist; refusing to upload empty state")
    db = sqlite3.connect(database)
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite index failed its integrity check")
        completed = db.execute("SELECT COUNT(*) FROM flights WHERE status='complete'").fetchone()[0]
        checkpoints = db.execute('SELECT scope,newest_date,"current_date",finished,updated_at FROM checkpoints').fetchall()
    finally:
        db.close()

    uploaded = 0
    for directory in ("raw_tracks", "details", "tracks"):
        base = root / directory
        if not base.exists():
            continue
        for source in sorted(path for path in base.iterdir() if path.is_file() and not path.name.endswith(".part")):
            upload_file(client, bucket, source, key(prefix, f"data/{directory}/{source.name}"))
            uploaded += 1

    flights_csv = root / "flights.csv"
    if flights_csv.is_file():
        upload_file(client, bucket, flights_csv, key(prefix, "state/flights.csv"))
    # Publish the index last so it never points at files that failed to upload.
    upload_file(client, bucket, database, key(prefix, "state/index.sqlite3"))
    manifest = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "complete_flights": completed,
        "files_uploaded_this_run": uploaded,
        "checkpoints": checkpoints,
    }
    client.put_object(
        Bucket=bucket,
        Key=key(prefix, "state/manifest.json"),
        Body=(json.dumps(manifest, indent=2) + "\n").encode(),
        ContentType="application/json",
    )
    print(f"Published checkpoint for {completed} complete flights")


def check(client, bucket: str, prefix: str) -> None:
    response = client.list_objects_v2(Bucket=bucket, Prefix=key(prefix, "state/"), MaxKeys=10)
    objects = [(item["Key"], item["Size"]) for item in response.get("Contents", [])]
    print(json.dumps({"bucket": bucket, "state_objects": objects}, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pull", "push", "check"))
    parser.add_argument("--output", default="weglide_archive_data")
    parser.add_argument("--prefix", default="north-america-v1")
    args = parser.parse_args()
    client, bucket = client_and_bucket()
    root = Path(args.output)
    if args.command == "pull":
        pull(client, bucket, args.prefix, root)
    elif args.command == "push":
        push(client, bucket, args.prefix, root)
    else:
        check(client, bucket, args.prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
