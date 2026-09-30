#!/usr/bin/env python3
"""Export render-ready polylines for every archived flight crossing New England."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import time

import numpy as np
from shapely.geometry import LineString
from shapely.ops import transform

from new_england_heatmap import build_geography, list_track_keys, load_track
from r2_storage import client_and_bucket


KEY_PATTERN = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})_(?P<id>\d+)\.json(?:\.gz)?$")


def identity_from_key(object_key: str) -> tuple[str, int]:
    match = KEY_PATTERN.search(object_key)
    if not match:
        raise ValueError(f"Cannot read flight date/id from {object_key}")
    return match.group("date"), int(match.group("id"))


def qualifying_polyline(points, geography, simplify_m: float) -> np.ndarray | None:
    if len(points) < 2:
        return None
    line = LineString(points)
    if not line.intersects(geography["new_england_lonlat"]):
        return None
    projected = transform(geography["forward"].transform, line)
    if simplify_m:
        projected = projected.simplify(simplify_m, preserve_topology=False)
    simplified = transform(geography["reverse"].transform, projected)
    coordinates = np.asarray(simplified.coords, dtype=np.float32)
    return coordinates if len(coordinates) >= 2 else None


def write_bundle(path: Path, tracks: list[tuple[str, int, np.ndarray]], snapshot_count: int,
                 failed_objects: int, simplify_m: float, runtime_seconds: float) -> dict:
    tracks.sort(key=lambda item: (item[0], item[1]))
    offsets = np.zeros(len(tracks) + 1, dtype=np.int64)
    for index, (_, _, coordinates) in enumerate(tracks, 1):
        offsets[index] = offsets[index - 1] + len(coordinates)
    coordinates = (np.concatenate([item[2] for item in tracks]).astype(np.float32)
                   if tracks else np.empty((0, 2), dtype=np.float32))
    dates = np.asarray([item[0] for item in tracks], dtype="U10")
    flight_ids = np.asarray([item[1] for item in tracks], dtype=np.int64)
    np.savez_compressed(path, coordinates=coordinates, offsets=offsets,
                        scoring_dates=dates, flight_ids=flight_ids)
    metadata = {
        "complete": True,
        "snapshot_count": snapshot_count,
        "qualifying_flights": len(tracks),
        "failed_objects": failed_objects,
        # NumPy 2.x has no minimum/maximum ufunc loop for Unicode arrays.
        # Tracks are sorted lexicographically by ISO date above, so read the
        # range from the sorted Python records.
        "date_start": tracks[0][0] if tracks else None,
        "date_end": tracks[-1][0] if tracks else None,
        "coordinate_count": int(len(coordinates)),
        "simplification_metres": simplify_m,
        "runtime_seconds": runtime_seconds,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "selection": "Track polyline intersects CT, RI, MA, VT, NH, or ME.",
        "flight_data_source": "WeGlide public viewer flight tracks (weglide.org)",
    }
    path.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="north-america-v1")
    parser.add_argument("--output", default="new_england_tracks.npz")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=240)
    parser.add_argument("--simplify-metres", type=float, default=250.0)
    args = parser.parse_args()
    if args.workers < 1 or args.batch_size < 1 or args.simplify_metres < 0:
        parser.error("workers/batch-size must be positive and simplification nonnegative")

    started = time.monotonic()
    geography = build_geography(5.0, 40.0)
    client, bucket = client_and_bucket()
    keys = list_track_keys(client, bucket, args.prefix)
    tracks: list[tuple[str, int, np.ndarray]] = []
    failures = 0
    for batch_start in range(0, len(keys), args.batch_size):
        batch = keys[batch_start:batch_start + args.batch_size]
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(load_track, client, bucket, object_key): object_key
                       for object_key in batch}
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    object_key = futures.pop(future)
                    try:
                        _, points = future.result()
                        coordinates = qualifying_polyline(points, geography, args.simplify_metres)
                        if coordinates is not None:
                            scoring_date, flight_id = identity_from_key(object_key)
                            tracks.append((scoring_date, flight_id, coordinates))
                    except Exception as exc:
                        failures += 1
                        print(f"Failed {object_key}: {type(exc).__name__}: {exc}", flush=True)
        processed = batch_start + len(batch)
        elapsed = time.monotonic() - started
        print(f"Processed {processed:,}/{len(keys):,}; qualifying={len(tracks):,}; "
              f"failures={failures:,}; {processed / elapsed:.1f} tracks/s", flush=True)

    output = Path(args.output)
    metadata = write_bundle(output, tracks, len(keys), failures, args.simplify_metres,
                            time.monotonic() - started)
    print(json.dumps(metadata, indent=2))
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
