#!/usr/bin/env python3
"""Build a distance-decay heat map from R2 flight tracks crossing New England."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import gzip
import io
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "weglide-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
import numpy as np
from pyproj import Transformer
from scipy.ndimage import distance_transform_edt
from shapely.geometry import LineString, box, shape
from shapely.ops import transform, unary_union

from r2_storage import client_and_bucket, key


NEW_ENGLAND = ("CT", "RI", "MA", "VT", "NH", "ME")
BOUNDARY_PATH = Path(__file__).parent / "data" / "cb_2025_new_england_states_500k.geojson"
BOUNDARY_SOURCE = "U.S. Census Bureau 2025 Cartographic Boundary Files, states, 1:500,000"
ANALYSIS_VERSION = 3


def load_state_geometries(path: Path = BOUNDARY_PATH):
    payload = json.loads(path.read_text(encoding="utf-8"))
    states = {}
    for feature in payload["features"]:
        abbreviation = feature["properties"]["STUSPS"]
        if abbreviation in NEW_ENGLAND:
            states[abbreviation] = shape(feature["geometry"]).buffer(0)
    missing = set(NEW_ENGLAND) - set(states)
    if missing:
        raise RuntimeError(f"Official boundary file is missing states: {sorted(missing)}")
    return states


def build_geography(cell_km: float, radius_km: float):
    forward = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    reverse = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)
    states_lonlat = load_state_geometries()
    new_england_lonlat = unary_union(list(states_lonlat.values()))
    states_xy = {abbr: transform(forward.transform, geometry) for abbr, geometry in states_lonlat.items()}
    new_england_xy = unary_union(list(states_xy.values()))
    cell_m = cell_km * 1000.0
    radius_m = radius_km * 1000.0
    min_x, min_y, max_x, max_y = new_england_xy.bounds
    min_x = math.floor((min_x - radius_m) / cell_m) * cell_m
    min_y = math.floor((min_y - radius_m) / cell_m) * cell_m
    max_x = math.ceil((max_x + radius_m) / cell_m) * cell_m
    max_y = math.ceil((max_y + radius_m) / cell_m) * cell_m
    width = int(round((max_x - min_x) / cell_m))
    height = int(round((max_y - min_y) / cell_m))
    return {
        "forward": forward,
        "reverse": reverse,
        "states_lonlat": states_lonlat,
        "states_xy": states_xy,
        "new_england_lonlat": new_england_lonlat,
        "new_england_xy": new_england_xy,
        "cell_m": cell_m,
        "radius_m": radius_m,
        "min_x": min_x,
        "min_y": min_y,
        "max_x": max_x,
        "max_y": max_y,
        "width": width,
        "height": height,
        "window": box(min_x - radius_m, min_y - radius_m,
                      max_x + radius_m, max_y + radius_m),
    }


def list_track_keys(client, bucket: str, prefix: str) -> list[str]:
    remote_prefix = key(prefix, "data/raw_tracks/")
    result: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=remote_prefix):
        result.extend(item["Key"] for item in page.get("Contents", [])
                      if item["Key"].endswith((".json", ".json.gz")))
    return sorted(result)


def load_track(client, bucket: str, object_key: str):
    response = client.get_object(Bucket=bucket, Key=object_key)
    body = response["Body"].read()
    if object_key.endswith(".gz"):
        body = gzip.decompress(body)
    payload = json.loads(body)
    coordinates = payload.get("geom", {}).get("coordinates", []) if isinstance(payload, dict) else []
    points = []
    for coordinate in coordinates:
        if (isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2
                and isinstance(coordinate[0], (int, float))
                and isinstance(coordinate[1], (int, float))
                and math.isfinite(coordinate[0]) and math.isfinite(coordinate[1])
                and -180 <= coordinate[0] <= 180 and -90 <= coordinate[1] <= 90):
            points.append((float(coordinate[0]), float(coordinate[1])))
    return object_key, points


def rasterize_line(line, geography) -> tuple[np.ndarray, np.ndarray]:
    """Return unique grid row/column indexes touched by a projected line."""
    cell_m = geography["cell_m"]
    min_x, min_y = geography["min_x"], geography["min_y"]
    width, height = geography["width"], geography["height"]
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    geometries = list(line.geoms) if hasattr(line, "geoms") else [line]
    for geometry in geometries:
        if not hasattr(geometry, "coords"):
            continue
        coords = np.asarray(geometry.coords, dtype=float)
        if len(coords) < 2:
            continue
        grid_x = (coords[:, 0] - min_x) / cell_m
        grid_y = (coords[:, 1] - min_y) / cell_m
        for start in range(len(coords) - 1):
            dx = grid_x[start + 1] - grid_x[start]
            dy = grid_y[start + 1] - grid_y[start]
            steps = max(1, int(math.ceil(max(abs(dx), abs(dy)) * 2)))
            fractions = np.linspace(0.0, 1.0, steps + 1)
            cols.append(np.floor(grid_x[start] + fractions * dx).astype(np.int32))
            rows.append(np.floor(grid_y[start] + fractions * dy).astype(np.int32))
    if not rows:
        return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32)
    row = np.concatenate(rows)
    col = np.concatenate(cols)
    valid = (row >= 0) & (row < height) & (col >= 0) & (col < width)
    flat = np.unique(row[valid].astype(np.int64) * width + col[valid])
    return (flat // width).astype(np.int32), (flat % width).astype(np.int32)


def add_track_contribution(distance_heat: np.ndarray, crossing_heat: np.ndarray,
                           points, geography) -> bool:
    if len(points) < 2:
        return False
    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    ne_bounds = geography["new_england_lonlat"].bounds
    if (max(longitudes) < ne_bounds[0] or min(longitudes) > ne_bounds[2]
            or max(latitudes) < ne_bounds[1] or min(latitudes) > ne_bounds[3]):
        return False
    line_lonlat = LineString(points)
    if not line_lonlat.intersects(geography["new_england_lonlat"]):
        return False
    line_xy = transform(geography["forward"].transform, line_lonlat)
    relevant = line_xy.intersection(geography["window"])
    if relevant.is_empty:
        return False
    rows, cols = rasterize_line(relevant, geography)
    if len(rows) == 0:
        return False

    # rasterize_line returns unique cells, so one flight adds exactly one count
    # even if it contains dense fixes or loops repeatedly inside the same cell.
    crossing_heat[rows, cols] += 1

    margin = int(math.ceil(geography["radius_m"] / geography["cell_m"])) + 1
    row0 = max(0, int(rows.min()) - margin)
    row1 = min(distance_heat.shape[0], int(rows.max()) + margin + 1)
    col0 = max(0, int(cols.min()) - margin)
    col1 = min(distance_heat.shape[1], int(cols.max()) + margin + 1)
    occupied = np.zeros((row1 - row0, col1 - col0), dtype=bool)
    occupied[rows - row0, cols - col0] = True
    distance_m = distance_transform_edt(~occupied, sampling=geography["cell_m"])
    contribution = np.maximum(0.0, 1.0 - distance_m / geography["radius_m"])
    distance_heat[row0:row1, col0:col1] += contribution.astype(np.float32)
    return True


def checkpoint_payload(parameters, snapshot_count, next_index, qualifying, failures, started_at):
    return {
        "analysis_version": ANALYSIS_VERSION,
        "parameters": parameters,
        "snapshot_count": snapshot_count,
        "next_index": next_index,
        "qualifying_flights": qualifying,
        "failed_objects": failures,
        "started_at": started_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def put_bytes(client, bucket: str, object_key: str, body: bytes, content_type: str) -> None:
    client.put_object(Bucket=bucket, Key=object_key, Body=body, ContentType=content_type)


def save_state(client, bucket: str, analysis_prefix: str, distance_heat: np.ndarray,
               crossing_heat: np.ndarray, progress: dict) -> None:
    buffer = io.BytesIO()
    np.save(buffer, distance_heat, allow_pickle=False)
    put_bytes(client, bucket, key(analysis_prefix, "state/distance_heat.npy"), buffer.getvalue(),
              "application/octet-stream")
    buffer = io.BytesIO()
    np.save(buffer, crossing_heat, allow_pickle=False)
    put_bytes(client, bucket, key(analysis_prefix, "state/crossing_heat.npy"), buffer.getvalue(),
              "application/octet-stream")
    put_bytes(client, bucket, key(analysis_prefix, "state/progress.json"),
              (json.dumps(progress, indent=2) + "\n").encode(), "application/json")


def save_snapshot(client, bucket: str, analysis_prefix: str, keys: list[str]) -> None:
    body = gzip.compress((json.dumps(keys) + "\n").encode(), compresslevel=6)
    put_bytes(client, bucket, key(analysis_prefix, "state/snapshot_keys.json.gz"), body,
              "application/gzip")


def get_bytes(client, bucket: str, object_key: str) -> bytes | None:
    try:
        return client.get_object(Bucket=bucket, Key=object_key)["Body"].read()
    except Exception as exc:
        response = getattr(exc, "response", {})
        code = response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def restore_state(client, bucket: str, analysis_prefix: str, parameters: dict):
    progress_body = get_bytes(client, bucket, key(analysis_prefix, "state/progress.json"))
    snapshot_body = get_bytes(client, bucket, key(analysis_prefix, "state/snapshot_keys.json.gz"))
    distance_body = get_bytes(client, bucket, key(analysis_prefix, "state/distance_heat.npy"))
    crossing_body = get_bytes(client, bucket, key(analysis_prefix, "state/crossing_heat.npy"))
    if not (progress_body and snapshot_body and distance_body and crossing_body):
        return None
    progress = json.loads(progress_body)
    if (progress.get("analysis_version") != ANALYSIS_VERSION
            or progress.get("parameters") != parameters):
        print("Stored analysis uses different parameters; starting a fresh snapshot")
        return None
    keys = json.loads(gzip.decompress(snapshot_body))
    distance_heat = np.load(io.BytesIO(distance_body), allow_pickle=False)
    crossing_heat = np.load(io.BytesIO(crossing_body), allow_pickle=False)
    if progress.get("snapshot_count") != len(keys):
        raise RuntimeError("Stored heat-map snapshot count is inconsistent")
    return keys, distance_heat, crossing_heat, progress


def grid_coordinates(geography):
    x_centers = geography["min_x"] + (np.arange(geography["width"]) + 0.5) * geography["cell_m"]
    y_centers = geography["min_y"] + (np.arange(geography["height"]) + 0.5) * geography["cell_m"]
    xx, yy = np.meshgrid(x_centers, y_centers)
    return geography["reverse"].transform(xx, yy)


def write_grid_csv(path: Path, grid: np.ndarray, geography, value_name: str) -> None:
    lon, lat = grid_coordinates(geography)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(f"longitude,latitude,{value_name}\n")
        for longitude, latitude, value in zip(lon.ravel(), lat.ravel(), grid.ravel()):
            handle.write(f"{longitude:.6f},{latitude:.6f},{float(value):.6f}\n")


def draw_map(ax, grid: np.ndarray, geography, title: str, subtitle: str,
             colorbar_label: str, interpolation: str):
    positive = grid[grid > 0]
    vmin = max(0.25, float(np.percentile(positive, 5))) if positive.size else 0.25
    vmax = float(np.percentile(positive, 99.5)) if positive.size else 1.0
    if vmax <= vmin:
        vmax = vmin + 1.0
    masked = np.ma.masked_less_equal(grid, 0)
    ax.set_facecolor("#eef2f3")
    image = ax.imshow(masked, origin="lower",
                      extent=(geography["min_x"], geography["max_x"],
                              geography["min_y"], geography["max_y"]),
                      cmap="inferno", norm=LogNorm(vmin=vmin, vmax=vmax),
                      interpolation=interpolation, alpha=0.88)
    for abbreviation, geometry in geography["states_xy"].items():
        parts = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
        for part in parts:
            x, y = part.exterior.xy
            ax.plot(x, y, color="white", linewidth=1.6, alpha=0.95)
            ax.plot(x, y, color="#25343b", linewidth=0.55, alpha=0.9)
        point = geometry.representative_point()
        ax.text(point.x, point.y, abbreviation, ha="center", va="center",
                fontsize=8, weight="bold", color="white",
                bbox={"boxstyle": "round,pad=0.15", "facecolor": "#25343b", "alpha": 0.7,
                      "edgecolor": "none"})
    colorbar = ax.figure.colorbar(image, ax=ax, fraction=0.035, pad=0.02)
    colorbar.set_label(colorbar_label)
    ax.set_title(title, fontsize=15, weight="bold", pad=14)
    ax.text(0.5, 1.005, subtitle, transform=ax.transAxes,
            ha="center", va="bottom", fontsize=9)
    ax.text(0.01, 0.01, "Boundary: U.S. Census Bureau 2025, 1:500,000",
            transform=ax.transAxes, fontsize=7.5, color="#25343b",
            bbox={"facecolor": "white", "alpha": 0.78, "edgecolor": "none", "pad": 3})
    ax.set_xlim(geography["min_x"], geography["max_x"])
    ax.set_ylim(geography["min_y"], geography["max_y"])
    ax.set_aspect("equal")
    ax.axis("off")


def render_outputs(output: Path, distance_heat: np.ndarray, crossing_heat: np.ndarray,
                   geography, metadata: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output / "new_england_distance_heatmap.npz", heat=distance_heat)
    np.savez_compressed(output / "new_england_crossings_heatmap.npz", crossings=crossing_heat)
    np.savez_compressed(output / "new_england_heatmaps.npz",
                        distance_score=distance_heat, crossing_count=crossing_heat)
    (output / "new_england_heatmap.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    write_grid_csv(output / "new_england_distance_heatmap.csv.gz", distance_heat,
                   geography, "distance_score")
    write_grid_csv(output / "new_england_crossings_heatmap.csv.gz", crossing_heat,
                   geography, "crossing_flights")

    common_subtitle = (f"{metadata['qualifying_flights']:,} flights · "
                       f"{metadata['parameters']['cell_km']:g} km cells")
    fig, ax = plt.subplots(figsize=(10.5, 11), dpi=180)
    draw_map(ax, distance_heat, geography,
             "WeGlide distance influence through New England",
             common_subtitle + f" · linear decay to zero at {metadata['parameters']['radius_km']:g} km",
             "Accumulated flight score (log color scale)", "bilinear")
    fig.tight_layout()
    fig.savefig(output / "new_england_distance_heatmap.png", bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10.5, 11), dpi=180)
    draw_map(ax, crossing_heat, geography,
             "WeGlide flight-path cell crossings through New England",
             common_subtitle + " · one count per flight per crossed cell",
             "Flights crossing cell (log color scale)", "nearest")
    fig.tight_layout()
    fig.savefig(output / "new_england_crossings_heatmap.png", bbox_inches="tight")
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(18, 9.5), dpi=170)
    draw_map(axes[0], distance_heat, geography, "Distance influence",
             f"1.0 on track; zero at {metadata['parameters']['radius_km']:g} km",
             "Accumulated score (log)", "bilinear")
    draw_map(axes[1], crossing_heat, geography, "Direct cell crossings",
             "One count per flight per crossed cell",
             "Crossing flights (log)", "nearest")
    fig.suptitle(f"WeGlide New England heat-map comparison · {common_subtitle}",
                 fontsize=17, weight="bold")
    fig.tight_layout()
    fig.savefig(output / "new_england_heatmap_comparison.png", bbox_inches="tight")
    plt.close(fig)

    # Preserve the original filename as the distance-decay product for existing links.
    shutil.copyfile(output / "new_england_distance_heatmap.png",
                    output / "new_england_heatmap.png")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="north-america-v1")
    parser.add_argument("--analysis-prefix", default="analysis/new-england-heatmaps-v2")
    parser.add_argument("--output", default="heatmap-output")
    parser.add_argument("--cell-km", type=float, default=5.0)
    parser.add_argument("--radius-km", type=float, default=40.0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=240)
    parser.add_argument("--max-runtime-minutes", type=float, default=110.0)
    parser.add_argument("--max-objects", type=int, default=0, help="Testing limit; zero processes snapshot")
    parser.add_argument("--fresh", action="store_true", help="Replace any prior analysis checkpoint")
    args = parser.parse_args()
    if args.cell_km <= 0 or args.radius_km <= 0 or args.workers < 1 or args.batch_size < 1:
        parser.error("cell/radius/workers/batch-size must be positive")

    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    deadline = started + args.max_runtime_minutes * 60
    parameters = {"cell_km": args.cell_km, "radius_km": args.radius_km,
                  "products": ["linear_nearest_track", "direct_cell_crossings"],
                  "new_england_states": list(NEW_ENGLAND)}
    geography = build_geography(args.cell_km, args.radius_km)
    client, bucket = client_and_bucket()

    restored = None if args.fresh or args.max_objects else restore_state(
        client, bucket, args.analysis_prefix, parameters)
    if restored:
        keys, distance_heat, crossing_heat, progress = restored
        next_index = int(progress["next_index"])
        qualifying = int(progress["qualifying_flights"])
        failures = int(progress["failed_objects"])
        started_at = progress["started_at"]
        expected_shape = (geography["height"], geography["width"])
        if distance_heat.shape != expected_shape or crossing_heat.shape != expected_shape:
            raise RuntimeError("Stored heat-map grid shape is inconsistent")
        print(f"Resuming fixed snapshot at {next_index:,}/{len(keys):,} objects")
    else:
        keys = list_track_keys(client, bucket, args.prefix)
        if args.max_objects:
            keys = keys[:args.max_objects]
        print(f"Snapshot contains {len(keys):,} raw track objects")
        save_snapshot(client, bucket, args.analysis_prefix, keys)
        distance_heat = np.zeros((geography["height"], geography["width"]), dtype=np.float32)
        crossing_heat = np.zeros((geography["height"], geography["width"]), dtype=np.uint32)
        next_index = qualifying = failures = 0

    # A fixed snapshot makes the result reproducible while collection continues.
    for batch_start in range(next_index, len(keys), args.batch_size):
        if time.monotonic() >= deadline - 120:
            break
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
                        if add_track_contribution(distance_heat, crossing_heat, points, geography):
                            qualifying += 1
                    except Exception as exc:  # preserve useful partial output and a bounded run
                        failures += 1
                        print(f"Failed {object_key}: {type(exc).__name__}: {exc}")
        next_index = batch_start + len(batch)
        progress = checkpoint_payload(parameters, len(keys), next_index, qualifying, failures, started_at)
        save_state(client, bucket, args.analysis_prefix, distance_heat, crossing_heat, progress)
        elapsed = time.monotonic() - started
        rate = next_index / elapsed if elapsed else 0
        print(f"Processed {next_index:,}/{len(keys):,}; qualifying={qualifying:,}; "
              f"failures={failures:,}; {rate:.1f} tracks/s")

    complete = next_index == len(keys)
    elapsed_seconds = time.monotonic() - started
    metadata = checkpoint_payload(parameters, len(keys), next_index, qualifying, failures, started_at)
    metadata.update({
        "complete": complete,
        "runtime_seconds": elapsed_seconds,
        "grid_width": geography["width"],
        "grid_height": geography["height"],
        "score_definition": ("For each qualifying flight and cell: max(0, 1 - "
                             f"nearest_track_distance_km / {args.radius_km:g}). "
                             "Contributions are summed across flights."),
        "crossing_definition": ("Each qualifying flight contributes exactly one count to each "
                                "5 km cell touched by its rasterized path, and zero otherwise."),
        "projection": "EPSG:5070",
        "boundary_source": BOUNDARY_SOURCE,
    })
    render_outputs(Path(args.output), distance_heat, crossing_heat, geography, metadata)
    save_state(client, bucket, args.analysis_prefix, distance_heat, crossing_heat, metadata)
    for path in Path(args.output).iterdir():
        content_type = "application/octet-stream"
        if path.suffix == ".png":
            content_type = "image/png"
        elif path.suffix == ".json":
            content_type = "application/json"
        elif path.name.endswith(".csv.gz"):
            content_type = "application/gzip"
        put_bytes(client, bucket, key(args.analysis_prefix, f"latest/{path.name}"),
                  path.read_bytes(), content_type)
    print(json.dumps(metadata, indent=2))
    return 0 if complete and failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
