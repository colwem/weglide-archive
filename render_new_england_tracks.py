#!/usr/bin/env python3
"""Render New England flight tracks alone and over a georeferenced Condor map."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import struct

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.colors import LogNorm
import numpy as np
from PIL import Image
from pyproj import Transformer

from new_england_heatmap import build_geography


ATTRIBUTION = "Flight data: WeGlide public viewer (weglide.org)"


def display_date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def prominent_period(ax, metadata: dict) -> None:
    ax.text(0.5, 1.012,
            f"{metadata['qualifying_flights']:,} flights  •  "
            f"{display_date(metadata['date_start'])} – {display_date(metadata['date_end'])}",
            transform=ax.transAxes, ha="center", va="bottom", fontsize=13, weight="bold",
            color="#26343a")


def read_condor_extent(trn_path: Path, width: int, height: int):
    with trn_path.open("rb") as handle:
        header = handle.read(36)
    trn_width, trn_height = struct.unpack_from("<II", header, 0)
    resolution = abs(struct.unpack_from("<f", header, 8)[0])
    east_edge, south_edge = struct.unpack_from("<ff", header, 20)
    zone = struct.unpack_from("<I", header, 28)[0]
    hemisphere = chr(struct.unpack_from("<I", header, 32)[0])
    if (trn_width, trn_height) != (width, height) or hemisphere.upper() != "N":
        raise ValueError("Unsupported or mismatched Condor terrain header")
    return (east_edge - width * resolution, east_edge,
            south_edge, south_edge + height * resolution, zone, resolution)


def load_tracks(bundle_path: Path):
    bundle = np.load(bundle_path, allow_pickle=False)
    coordinates = bundle["coordinates"]
    offsets = bundle["offsets"]
    segments = [coordinates[offsets[index]:offsets[index + 1]]
                for index in range(len(offsets) - 1)]
    return bundle, segments


def footer(fig, metadata: dict, extra: str = "", color: str = "#33434a") -> None:
    text = (f"{metadata['date_start']} to {metadata['date_end']} · "
            f"{metadata['qualifying_flights']:,} flights · {ATTRIBUTION}")
    if extra:
        text += f" · {extra}"
    fig.text(0.5, 0.012, text, ha="center", va="bottom", fontsize=8, color=color)


def render_flight_line_overlay(segments, metadata: dict, bitmap_path: Path, trn_path: Path,
                               output: Path) -> None:
    with Image.open(bitmap_path) as source:
        bitmap = np.asarray(source.convert("RGB"))
    x0, x1, y0, y1, zone, _ = read_condor_extent(
        trn_path, bitmap.shape[1], bitmap.shape[0])
    transformer = Transformer.from_crs(4326, 32600 + zone, always_xy=True)
    projected = [np.column_stack(transformer.transform(line[:, 0], line[:, 1]))
                 for line in segments]
    clipped = [line for line in projected
               if len(line) > 1 and line[:, 0].max() >= x0 and line[:, 0].min() <= x1
               and line[:, 1].max() >= y0 and line[:, 1].min() <= y1]
    fig, ax = plt.subplots(figsize=(10.5, 12), dpi=180)
    ax.imshow(bitmap, extent=(x0, x1, y0, y1), origin="upper")
    ax.add_collection(LineCollection(clipped, colors="#d000ff", linewidths=0.38,
                                     alpha=0.13, rasterized=True))
    ax.add_collection(LineCollection(clipped, colors="#ffffff", linewidths=0.13,
                                     alpha=0.16, rasterized=True))
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("WeGlide flights over New England", fontsize=19, weight="bold", pad=31)
    prominent_period(ax, metadata)
    fig.text(0.5, 0.012, ATTRIBUTION, ha="center", va="bottom", fontsize=8,
             color="#33434a")
    fig.tight_layout(rect=(0, 0.025, 1, 0.98))
    fig.savefig(output, bbox_inches="tight")
    plt.close(fig)


def render_heatmap_overlay(metadata: dict, crossing_grid_path: Path, bitmap_path: Path,
                           trn_path: Path, output: Path) -> None:
    with Image.open(bitmap_path) as source:
        bitmap = np.asarray(source.convert("RGB"))
    x0, x1, y0, y1, zone, resolution = read_condor_extent(
        trn_path, bitmap.shape[1], bitmap.shape[0])
    grid = np.load(crossing_grid_path, allow_pickle=False)["crossings"]
    geography = build_geography(5.0, 40.0)
    expected_shape = (geography["height"], geography["width"])
    if grid.shape != expected_shape:
        raise ValueError(f"Crossing grid is {grid.shape}, expected {expected_shape}")
    x_edges = geography["min_x"] + np.arange(geography["width"] + 1) * geography["cell_m"]
    y_edges = geography["min_y"] + np.arange(geography["height"] + 1) * geography["cell_m"]
    xx, yy = np.meshgrid(x_edges, y_edges)
    transformer = Transformer.from_crs(5070, 32600 + zone, always_xy=True)
    map_x, map_y = transformer.transform(xx, yy)
    masked = np.ma.masked_less_equal(grid, 0)
    positive = grid[grid > 0]
    vmax = max(2.0, float(np.percentile(positive, 99.5)))

    fig, ax = plt.subplots(figsize=(10.5, 12), dpi=180)
    ax.imshow(bitmap, extent=(x0, x1, y0, y1), origin="upper")
    heat = ax.pcolormesh(map_x, map_y, masked, cmap="plasma",
                         norm=LogNorm(vmin=1.0, vmax=vmax), shading="flat",
                         alpha=0.38, edgecolors="none", rasterized=True)
    colorbar = fig.colorbar(heat, ax=ax, fraction=0.035, pad=0.018)
    colorbar.set_label("Flights crossing each 5 km cell (log scale)", fontsize=9)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("WeGlide flights over New England", fontsize=19, weight="bold", pad=31)
    prominent_period(ax, metadata)
    fig.text(0.5, 0.012, ATTRIBUTION, ha="center", va="bottom", fontsize=8,
             color="#33434a")
    fig.tight_layout(rect=(0, 0.025, 1, 0.98))
    fig.savefig(output, bbox_inches="tight")
    plt.close(fig)


def render_all_tracks(segments, metadata: dict, output: Path) -> None:
    transformer = Transformer.from_crs(4326, 5070, always_xy=True)
    projected = [np.column_stack(transformer.transform(line[:, 0], line[:, 1]))
                 for line in segments]
    all_points = np.concatenate(projected)
    x0, y0 = all_points.min(axis=0)
    x1, y1 = all_points.max(axis=0)
    pad = 15000
    fig, ax = plt.subplots(figsize=(11, 11), dpi=200, facecolor="#061018")
    ax.set_facecolor("#061018")
    ax.add_collection(LineCollection(projected, colors="#37d9ff", linewidths=0.28,
                                     alpha=0.055, rasterized=True))
    ax.add_collection(LineCollection(projected, colors="#fff36a", linewidths=0.08,
                                     alpha=0.075, rasterized=True))
    ax.set_xlim(x0 - pad, x1 + pad)
    ax.set_ylim(y0 - pad, y1 + pad)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Every archived flight crossing New England", color="white",
                 fontsize=17, weight="bold", pad=12)
    footer(fig, metadata, color="#b9cbd3")
    fig.tight_layout(rect=(0, 0.028, 1, 0.98))
    fig.savefig(output, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def render_crossing_annotation(source: Path, metadata: dict, output: Path) -> None:
    image = plt.imread(source)
    fig, ax = plt.subplots(figsize=(10.5, 11.6), dpi=180)
    ax.imshow(image)
    ax.axis("off")
    footer(fig, metadata, "5 km cells · one count per flight per crossed cell")
    fig.tight_layout(rect=(0, 0.025, 1, 1))
    fig.savefig(output, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracks", required=True)
    parser.add_argument("--condor-bitmap", required=True)
    parser.add_argument("--condor-trn")
    parser.add_argument("--crossing-map", required=True)
    parser.add_argument("--crossing-grid", required=True)
    parser.add_argument("--output", default="heatmap-results/presentation")
    args = parser.parse_args()
    bundle_path = Path(args.tracks)
    metadata = json.loads(bundle_path.with_suffix(".json").read_text(encoding="utf-8"))
    _, segments = load_tracks(bundle_path)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    bitmap_path = Path(args.condor_bitmap)
    trn_path = Path(args.condor_trn) if args.condor_trn else bitmap_path.with_name(
        bitmap_path.name.replace("_sect.bmp", ".trn"))
    render_flight_line_overlay(segments, metadata, bitmap_path, trn_path,
                               output / "new_england_flight_paths_over_relief_sectional.png")
    render_heatmap_overlay(metadata, Path(args.crossing_grid), bitmap_path, trn_path,
                           output / "new_england_heatmap_over_relief_sectional.png")
    render_all_tracks(segments, metadata, output / "new_england_all_flights.png")
    render_crossing_annotation(Path(args.crossing_map), metadata,
                               output / "new_england_crossings_annotated.png")
    print(json.dumps({"outputs": [str(path) for path in output.glob("*.png")], **metadata}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
