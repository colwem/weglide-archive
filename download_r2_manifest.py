#!/usr/bin/env python3
"""Download an R2 archive from a gzipped JSON-lines presigned manifest."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import json
from pathlib import Path, PurePosixPath
import shutil
import urllib.request


def safe_destination(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe manifest path: {relative!r}")
    return root.joinpath(*path.parts)


def download_one(root: Path, item: dict) -> tuple[str, str]:
    destination = safe_destination(root, item["path"])
    expected = int(item["size"])
    if destination.is_file() and destination.stat().st_size == expected:
        return "skipped", item["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(item["url"], headers={"User-Agent": "weglide-r2-restore/1"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)
    if partial.stat().st_size != expected:
        raise IOError(f"Size mismatch for {item['path']}: {partial.stat().st_size} != {expected}")
    partial.replace(destination)
    return "downloaded", item["path"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()
    with gzip.open(args.manifest, "rt", encoding="utf-8") as handle:
        items = [json.loads(line) for line in handle]
    required = sum(int(item["size"]) for item in items)
    args.output.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(args.output).free
    print(f"Archive: {len(items):,} objects, {required / 1e9:.1f} GB; free disk: {free / 1e9:.1f} GB")
    if free < required:
        raise RuntimeError("Not enough free disk space for a complete archive copy")
    counts = {"downloaded": 0, "skipped": 0}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(download_one, args.output, item) for item in items]
        for number, future in enumerate(as_completed(futures), 1):
            status, _ = future.result()
            counts[status] += 1
            if number % 100 == 0 or number == len(items):
                print(f"{number:,}/{len(items):,}: {counts['downloaded']:,} downloaded, "
                      f"{counts['skipped']:,} already present", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
