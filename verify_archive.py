#!/usr/bin/env python3
"""Fast integrity check suitable for a remote collector smoke test."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from pathlib import Path
import sqlite3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="weglide_archive_data")
    parser.add_argument("--minimum-flights", type=int, default=1)
    args = parser.parse_args()
    root = Path(args.output)
    db = sqlite3.connect(root / "index.sqlite3")
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity check failed")
        rows = db.execute("""SELECT id, point_count, raw_track_path, track_csv_path,
                             detail_path FROM flights WHERE status='complete'""").fetchall()
    finally:
        db.close()
    if len(rows) < args.minimum_flights:
        raise RuntimeError(f"Expected at least {args.minimum_flights} complete flight(s), found {len(rows)}")
    total_points = 0
    for flight_id, expected_points, raw_path, csv_path, detail_path in rows:
        raw_file, csv_file, detail_file = root / raw_path, root / csv_path, root / detail_path
        json_open = gzip.open if raw_file.suffix == ".gz" else open
        with json_open(raw_file, "rt", encoding="utf-8") as handle:
            track = json.load(handle)
        json_open = gzip.open if detail_file.suffix == ".gz" else open
        with json_open(detail_file, "rt", encoding="utf-8") as handle:
            detail = json.load(handle)
        csv_open = gzip.open if csv_file.suffix == ".gz" else open
        with csv_open(csv_file, "rt", encoding="utf-8", newline="") as handle:
            points = list(csv.DictReader(handle))
        if track.get("id") != flight_id or detail.get("id") != flight_id:
            raise RuntimeError(f"Flight {flight_id} has mismatched payload IDs")
        if len(points) != expected_points or len(track["geom"]["coordinates"]) != expected_points:
            raise RuntimeError(f"Flight {flight_id} has inconsistent point counts")
        total_points += expected_points
    print(f"Verified {len(rows)} complete flight(s), {total_points} normalized points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
