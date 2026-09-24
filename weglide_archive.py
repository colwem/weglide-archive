#!/usr/bin/env python3
"""Resumable public WeGlide North America track collector."""

from __future__ import annotations

import argparse
import asyncio
import csv
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import gzip
import json
import math
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import time
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


START_URL = "https://www.weglide.org/flight/map?continent=North%2520America,NA"
DETAIL_URL = "https://api.weglide.org/v1/flightdetail/{flight_id}"
TRACK_URL = "https://api.weglide.org/v1/flightdata/{flight_id}"
NE_US = ('US-CT', 'US-ME', 'US-MA', 'US-NH', 'US-RI', 'US-VT', 'US-NY', 'US-NJ', 'US-PA')
EASTERN_CANADA = ('CA-ON', 'CA-QC', 'CA-NB', 'CA-NS', 'CA-PE', 'CA-NL')


def area_regions(area: str) -> tuple[str, ...]:
    return () if area == 'na' else NE_US if area == 'ne-us' else NE_US + EASTERN_CANADA


def in_area(listing: dict[str, Any], regions: tuple[str, ...]) -> bool:
    return not regions or nested(listing, 'takeoff_airport', 'region') in regions


@contextmanager
def archive_lock(root: Path):
    """OS lock is released even on a crash; a stale filename is harmless."""
    handle = (root / 'collector.lock').open('a+b')
    handle.seek(0)
    if handle.read(1) == b'':
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError('Another collector is using this archive') from exc
    try:
        yield
    finally:
        handle.close()


class StopAccess(Exception):
    """Stop cleanly instead of retrying an access/throttling response."""


@dataclass
class FetchResult:
    status: int
    body: str
    retry_after: str | None


def utc_iso(epoch: float | int | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def nested(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def first_value(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def interpolate(values: list[Any] | None, position: float) -> Any:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    lo = int(position)
    hi = min(lo + 1, len(values) - 1)
    fraction = position - lo
    a, b = values[lo], values[hi]
    if a is None or b is None:
        return a if fraction < 0.5 else b
    try:
        return a * (1.0 - fraction) + b * fraction
    except TypeError:
        return a if fraction < 0.5 else b


def normalized_points(track: dict[str, Any]) -> Iterable[dict[str, Any]]:
    coords = nested(track, "geom", "coordinates") or []
    times = track.get("time") or []
    if not coords or not times:
        return
    arrays = {
        "altitude": track.get("alt"),
        "ground_altitude": track.get("ground_alt"),
        "engine_sensor": track.get("engine_sensor"),
        "fes_battery": track.get("fes_battery"),
        "fes_energy": track.get("fes_energy"),
        "fes_power": track.get("fes_power"),
    }
    coordinate_denominator = max(len(coords) - 1, 1)
    time_denominator = max(len(times) - 1, 0)
    for index, coordinate in enumerate(coords):
        position = index * time_denominator / coordinate_denominator
        timestamp = interpolate(times, position)
        values = {name: interpolate(array, index * max(len(array) - 1, 0) / coordinate_denominator)
                  if array else None for name, array in arrays.items()}
        altitude = values["altitude"]
        ground = values["ground_altitude"]
        yield {
            "timestamp_utc": utc_iso(timestamp),
            "unix_time": timestamp,
            "longitude": coordinate[0] if len(coordinate) > 0 else None,
            "latitude": coordinate[1] if len(coordinate) > 1 else None,
            "altitude_site_units": altitude,
            "ground_altitude_site_units": ground,
            "agl_site_units": altitude - ground if altitude is not None and ground is not None else None,
            "engine_sensor_raw": values["engine_sensor"],
            "fes_battery_raw": values["fes_battery"],
            "fes_energy_raw": values["fes_energy"],
            "fes_power_raw": values["fes_power"],
        }


TRACK_COLUMNS = [
    "timestamp_utc", "unix_time", "longitude", "latitude",
    "altitude_site_units", "ground_altitude_site_units", "agl_site_units",
    "engine_sensor_raw", "fes_battery_raw", "fes_energy_raw", "fes_power_raw",
]


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_track_csv(path: Path, track: dict[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    count = 0
    try:
        with temp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRACK_COLUMNS)
            writer.writeheader()
            for point in normalized_points(track):
                writer.writerow(point)
                count += 1
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return count


def set_query(url: str, **changes: Any) -> str:
    parsed = urlsplit(url)
    changed = {key: str(value) for key, value in changes.items() if value is not None}
    pairs = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key not in changed]
    pairs.extend(changed.items())
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), ""))


def month_floor(day: date) -> date:
    return day.replace(day=1)


def previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def month_last(day: date) -> date:
    next_month = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return next_month - timedelta(days=1)


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS flights (
            id INTEGER PRIMARY KEY,
            scoring_date TEXT,
            start_utc TEXT,
            end_utc TEXT,
            pilot TEXT,
            copilot TEXT,
            aircraft TEXT,
            registration TEXT,
            competition_id TEXT,
            airport TEXT,
            point_count INTEGER,
            raw_track_path TEXT,
            track_csv_path TEXT,
            detail_path TEXT,
            listing_json TEXT,
            downloaded_at TEXT,
            status TEXT NOT NULL,
            error TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS months (
            month TEXT PRIMARY KEY,
            completed_at TEXT NOT NULL,
            listed_flights INTEGER NOT NULL
        )
    """)
    db.execute("""CREATE TABLE IF NOT EXISTS checkpoints (
        scope TEXT PRIMARY KEY, newest_date TEXT NOT NULL, current_date TEXT NOT NULL,
        finished INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS listing_cache (
        id INTEGER PRIMARY KEY, scoring_date TEXT NOT NULL, airport_region TEXT,
        listing_json TEXT NOT NULL, seen_at TEXT NOT NULL)""")
    db.commit()
    return db


def checkpoint(db: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    scope = json.dumps({'continent': 'NA', 'oldest': args.stop_date}, sort_keys=True)
    row = db.execute('SELECT newest_date,"current_date",finished FROM checkpoints WHERE scope=?', (scope,)).fetchone()
    if row and not args.restart_scan:
        if args.start_date and args.start_date != row[0]:
            raise ValueError('This area has a saved start date; use --restart-scan to change it')
        newest, current, finished = row
    else:
        newest = args.start_date or date.today().isoformat()
        current, finished = newest, 0
    if date.fromisoformat(args.stop_date) > date.fromisoformat(newest):
        raise ValueError('--stop-date must not be later than --start-date')
    state = dict(scope=scope, newest_date=newest, current_date=current, finished=finished)
    save_checkpoint(db, state)
    return state


def save_checkpoint(db: sqlite3.Connection, state: dict[str, Any]) -> None:
    db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?)',
               (state['scope'], state['newest_date'], state['current_date'], state['finished'], utc_iso(time.time())))
    db.commit()


def scope_count(db: sqlite3.Connection, regions: tuple[str, ...], oldest: str, newest: str) -> int:
    sql = "SELECT COUNT(*) FROM flights WHERE status='complete' AND scoring_date BETWEEN ? AND ?"
    params: list[Any] = [oldest, newest]
    if regions:
        sql += " AND json_extract(listing_json, '$.takeoff_airport.region') IN (" + ','.join('?' for _ in regions) + ')'
        params.extend(regions)
    return db.execute(sql, params).fetchone()[0]


def day_url(template: str, day: str, skip: int, limit: int) -> str:
    # Remove optional map controls; keep only the verified NA geographic scope.
    parsed = urlsplit(template)
    return set_query(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', '')),
                     continent_id_in='NA', scoring_date_in=day, skip=skip, limit=limit,
                     order_by='-scoring_date', include_story='false', include_stats='false')


def validate_batch(batch: list[dict[str, Any]], day: str, seen: set[int]) -> None:
    if any(str(item.get('scoring_date')) != day for item in batch):
        raise ValueError('Site did not honor the exact-day filter; checkpoint retained')
    ids = [int(item['id']) for item in batch]
    if len(ids) != len(set(ids)) or (ids and all(i in seen for i in ids)):
        raise ValueError('Pagination repeated a page; checkpoint retained')


def completed_flight(db: sqlite3.Connection, flight_id: int) -> bool:
    row = db.execute("SELECT status FROM flights WHERE id = ?", (flight_id,)).fetchone()
    return bool(row and row[0] == "complete")


def write_flight(db: sqlite3.Connection, root: Path, listing: dict[str, Any],
                 detail: dict[str, Any], track: dict[str, Any]) -> None:
    flight_id = int(listing["id"])
    if any(int(payload.get("id", -1)) != flight_id for payload in (detail, track)):
        raise ValueError("Listing, detail and track flight IDs do not match")
    coords = nested(track, "geom", "coordinates") or []
    times = track.get("time") or []
    if nested(track, "geom", "type") != "LineString" or len(coords) < 2 or len(times) < 2:
        raise ValueError("Track must contain a LineString and at least two timestamps and coordinates")
    if any(not isinstance(t, (int, float)) or not math.isfinite(t) for t in times):
        raise ValueError("Track has invalid timestamps")
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Track timestamps must increase strictly")
    if any(len(c) < 2 or not (-180 <= c[0] <= 180 and -90 <= c[1] <= 90) for c in coords):
        raise ValueError("Track has invalid longitude/latitude")
    scoring_date = str(first_value(detail.get("scoring_date"), listing.get("scoring_date"), "unknown"))
    stem = f"{scoring_date}_{flight_id}"
    raw_track = root / "raw_tracks" / f"{stem}.json"
    detail_path = root / "details" / f"{stem}.json"
    track_csv = root / "tracks" / f"{stem}.csv"
    atomic_json(raw_track, track)
    atomic_json(detail_path, detail)
    point_count = atomic_track_csv(track_csv, track)

    pilot = first_value(nested(detail, "user", "name"), nested(listing, "user", "name"),
                        detail.get("user_name"), listing.get("user_name"))
    copilot = first_value(nested(detail, "co_user", "name"), detail.get("co_user_name"),
                          nested(listing, "co_user", "name"), listing.get("co_user_name"))
    aircraft = first_value(nested(detail, "aircraft", "name"), nested(listing, "aircraft", "name"),
                           detail.get("aircraft_name"), listing.get("aircraft_name"))
    airport = first_value(nested(detail, "takeoff_airport", "name"),
                          nested(listing, "takeoff_airport", "name"))
    times = track.get("time") or []
    start_utc = first_value(detail.get("takeoff_time"), listing.get("takeoff_time"),
                            utc_iso(times[0]) if times else None)
    end_utc = first_value(detail.get("landing_time"), listing.get("landing_time"),
                          utc_iso(times[-1]) if times else None)
    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        INSERT INTO flights (
            id, scoring_date, start_utc, end_utc, pilot, copilot, aircraft,
            registration, competition_id, airport, point_count, raw_track_path,
            track_csv_path, detail_path, listing_json, downloaded_at, status, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', NULL)
        ON CONFLICT(id) DO UPDATE SET
            scoring_date=excluded.scoring_date, start_utc=excluded.start_utc,
            end_utc=excluded.end_utc, pilot=excluded.pilot, copilot=excluded.copilot,
            aircraft=excluded.aircraft, registration=excluded.registration,
            competition_id=excluded.competition_id, airport=excluded.airport,
            point_count=excluded.point_count, raw_track_path=excluded.raw_track_path,
            track_csv_path=excluded.track_csv_path, detail_path=excluded.detail_path,
            listing_json=excluded.listing_json, downloaded_at=excluded.downloaded_at,
            status='complete', error=NULL
    """, (
        flight_id, scoring_date, start_utc, end_utc, pilot, copilot, aircraft,
        first_value(detail.get("registration"), listing.get("registration")),
        first_value(detail.get("competition_id"), listing.get("competition_id")),
        airport, point_count, raw_track.relative_to(root).as_posix(),
        track_csv.relative_to(root).as_posix(), detail_path.relative_to(root).as_posix(),
        json.dumps(listing, ensure_ascii=False, separators=(",", ":")), now,
    ))
    db.commit()


def write_error(db: sqlite3.Connection, flight_id: int, listing: dict[str, Any], error: str) -> None:
    db.execute("""
        INSERT INTO flights (id, scoring_date, listing_json, downloaded_at, status, error)
        VALUES (?, ?, ?, ?, 'error', ?)
        ON CONFLICT(id) DO UPDATE SET status='error', error=excluded.error,
            downloaded_at=excluded.downloaded_at, listing_json=excluded.listing_json
    """, (flight_id, listing.get("scoring_date"), json.dumps(listing, ensure_ascii=False),
          datetime.now(timezone.utc).isoformat(), error))
    db.commit()


def export_metadata(db: sqlite3.Connection, path: Path) -> None:
    columns = ["id", "scoring_date", "start_utc", "end_utc", "pilot", "copilot",
               "aircraft", "registration", "competition_id", "airport", "point_count",
               "raw_track_path", "track_csv_path", "detail_path", "downloaded_at", "status", "error"]
    temp = path.with_suffix(path.suffix + ".part")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(db.execute(f"SELECT {','.join(columns)} FROM flights ORDER BY scoring_date DESC, id DESC"))
    temp.replace(path)


def sync_to_r2(root: Path, prefix: str) -> None:
    command = [
        sys.executable,
        str(Path(__file__).with_name("r2_storage.py")),
        "push",
        "--output", str(root),
        "--prefix", prefix,
        "--prune-uploaded",
    ]
    print("Publishing an incremental R2 checkpoint...", flush=True)
    subprocess.run(command, check=True)
    print("Incremental R2 checkpoint published.", flush=True)


async def browser_fetch(page: Any, urls: list[str]) -> list[FetchResult]:
    results = await page.evaluate("""
        async (urls) => Promise.all(urls.map(async (url) => {
            let response;
            try {
                response = await fetch(url, {credentials: 'include', cache: 'no-store', signal: AbortSignal.timeout(60000)});
            } catch (error) {
                return {status: 0, body: `${error.name}: ${error.message}`, retry_after: null};
            }
            return {
                status: response.status,
                body: await response.text(),
                retry_after: response.headers.get('retry-after')
            };
        }))
    """, urls)
    output = [FetchResult(int(item["status"]), item["body"], item.get("retry_after"))
              for item in results]
    for result in output:
        if result.status == 0:
            raise StopAccess(f"Browser request failed ({result.body}); the browser may hide an HTTP refusal behind a CORS error. Inspect the diagnostics; --visible is available for troubleshooting.")
        if result.status in (401, 403, 429):
            extra = f" Retry-After: {result.retry_after}." if result.retry_after else ""
            raise StopAccess(f"Site returned HTTP {result.status}.{extra}")
    return output


async def discover_list_url(page: Any) -> str:
    loop = asyncio.get_running_loop()
    found: asyncio.Future[str] = loop.create_future()

    def observe(request: Any) -> None:
        url = request.url
        if "/v1/flight?" in url and "/flightdetail/" not in url and not found.done():
            found.set_result(url)

    page.on("request", observe)
    await page.goto(START_URL, wait_until="domcontentloaded", timeout=90_000)
    try:
        return await asyncio.wait_for(found, timeout=60)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Could not observe the flight-list request. Reload the page manually once, then retry.") from exc


async def fetch_json(page: Any, url: str) -> Any:
    result = (await browser_fetch(page, [url]))[0]
    if result.status != 200:
        raise RuntimeError(f"HTTP {result.status} for {urlsplit(url).path}")
    return json.loads(result.body)


def as_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "data"):
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
    raise ValueError("Unexpected flight-list JSON shape")


async def launch_context(playwright: Any, profile: Path, headless: bool) -> Any:
    profile.mkdir(parents=True, exist_ok=True)
    options = dict(user_data_dir=str(profile), headless=headless,
                   viewport={"width": 1440, "height": 1000}, locale="en-US")
    try:
        return await playwright.chromium.launch_persistent_context(channel="chrome", **options)
    except Exception:
        return await playwright.chromium.launch_persistent_context(**options)


async def collect(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    profile = (
        Path(args.profile).resolve()
        if args.profile
        else root.with_name(f"{root.name}_browser_profile")
    )
    db = open_database(root / "index.sqlite3")
    downloaded = attempted = consecutive_errors = 0
    last_synced_downloaded = 0
    state = checkpoint(db, args)
    priority_regions = area_regions(args.priority_area)
    deadline = (time.monotonic() + args.max_runtime_minutes * 60
                if args.max_runtime_minutes else None)

    def time_expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    try:
        with archive_lock(root):
            async with async_playwright() as playwright:
                context = await launch_context(playwright, profile, args.headless)
                page = context.pages[0] if context.pages else await context.new_page()
                page.on("console", lambda message: print(f"Browser: {message.text}", flush=True)
                        if message.type == "error" else None)
                page.on("requestfailed", lambda request: print(
                    f"Request failed: {request.method} {request.url}: {request.failure}", flush=True))
                try:
                    print("Opening the North America flight page...", flush=True)
                    list_template = await discover_list_url(page)
                    print(f"Observed listing: {list_template}", flush=True)
                    if dict(parse_qsl(urlsplit(list_template).query)).get("continent_id_in") != "NA":
                        raise ValueError("Observed listing lacks the expected North America filter")
                    if state['finished']:
                        print("Saved date range is already complete. Use --restart-scan to rescan it.", flush=True)
                        return 0
                    current = date.fromisoformat(state['current_date'])
                    oldest = date.fromisoformat(args.stop_date)
                    listing_requests = 0
                    while current >= oldest:
                        if time_expired():
                            print(f"Reached --max-runtime-minutes {args.max_runtime_minutes}; checkpoint retained.", flush=True)
                            return 0
                        day = current.isoformat()
                        print(f"Scanning North America for {day}...", flush=True)
                        listings: list[dict[str, Any]] = []
                        seen: set[int] = set()
                        offset = 0
                        while True:
                            if time_expired():
                                print(f"Reached --max-runtime-minutes {args.max_runtime_minutes}; checkpoint remains on {day}.", flush=True)
                                return 0
                            if listing_requests:
                                await asyncio.sleep(random.uniform(args.min_list_delay, args.max_list_delay))
                            batch = as_list(await fetch_json(page, day_url(list_template, day, offset, 100)))
                            listing_requests += 1
                            validate_batch(batch, day, seen)
                            if not batch:
                                break
                            for listing in batch:
                                flight_id = int(listing['id'])
                                if flight_id not in seen:
                                    listings.append(listing)
                                    seen.add(flight_id)
                                db.execute("INSERT OR REPLACE INTO listing_cache VALUES (?,?,?,?,?)", (
                                    flight_id, day, nested(listing, 'takeoff_airport', 'region'),
                                    json.dumps(listing, ensure_ascii=False, separators=(',', ':')),
                                    utc_iso(time.time())))
                            db.commit()
                            print(f"  listed {len(listings)} flights", flush=True)
                            if len(batch) < 100:
                                break
                            offset += len(batch)
                        listings.sort(key=lambda item: (not in_area(item, priority_regions), -int(item['id'])))
                        pending = [item for item in listings if not completed_flight(db, int(item['id']))]
                        print(f"  {len(pending)} pending; Northeast-priority={args.priority_area != 'na'}", flush=True)
                        for listing in pending:
                            if time_expired():
                                print(f"Reached --max-runtime-minutes {args.max_runtime_minutes}; checkpoint remains on {day}.", flush=True)
                                return 0
                            if args.max_flights and downloaded >= args.max_flights:
                                print(f"Reached --max-flights {args.max_flights}; checkpoint remains on {day}.", flush=True)
                                return 0
                            flight_id = int(listing['id'])
                            if attempted:
                                delay = random.uniform(args.min_delay, args.max_delay)
                                print(f"Waiting {delay:.1f}s before flight {flight_id}...", flush=True)
                                await asyncio.sleep(delay)
                            print(f"Downloading flight {flight_id}", flush=True)
                            attempted += 1
                            try:
                                detail_result, track_result = await browser_fetch(
                                    page, [DETAIL_URL.format(flight_id=flight_id), TRACK_URL.format(flight_id=flight_id)])
                                if detail_result.status != 200 or track_result.status != 200:
                                    raise RuntimeError(f"detail HTTP {detail_result.status}; track HTTP {track_result.status}")
                                detail, track = json.loads(detail_result.body), json.loads(track_result.body)
                                write_flight(db, root, listing, detail, track)
                                downloaded += 1
                                consecutive_errors = 0
                                export_metadata(db, root / "flights.csv")
                                print(f"Saved flight {flight_id}: {len(track['geom']['coordinates'])} points", flush=True)
                            except StopAccess:
                                raise
                            except Exception as exc:
                                message = f"{type(exc).__name__}: {exc}"
                                write_error(db, flight_id, listing, message)
                                print(f"Flight {flight_id} failed: {message}", file=sys.stderr, flush=True)
                                consecutive_errors += 1
                                if consecutive_errors >= 3:
                                    raise RuntimeError("Stopped after three consecutive flight errors") from exc
                            else:
                                if (args.r2_sync_every_flights and
                                        downloaded - last_synced_downloaded >= args.r2_sync_every_flights):
                                    sync_to_r2(root, args.r2_prefix)
                                    last_synced_downloaded = downloaded
                        current -= timedelta(days=1)
                        state['current_date'] = current.isoformat()
                        state['finished'] = int(current < oldest)
                        save_checkpoint(db, state)
                        print(f"Completed {day}; next checkpoint {state['current_date']}", flush=True)
                finally:
                    await context.close()
    except StopAccess as exc:
        export_metadata(db, root / "flights.csv")
        print(f"Stopped cleanly: {exc} Run the same command later to resume.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        export_metadata(db, root / "flights.csv")
        print("Stopped by user. Run the same command later to resume.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Stopped: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        export_metadata(db, root / "flights.csv")
        db.close()
    return 0


def inspect(args: argparse.Namespace) -> int:
    source = Path(args.file)
    with (gzip.open(source, "rt", encoding="utf-8") if source.suffix == ".gz"
          else source.open("r", encoding="utf-8")) as handle:
        track = json.load(handle)
    coords = nested(track, "geom", "coordinates") or []
    times = track.get("time") or []
    gaps = [b - a for a, b in zip(times, times[1:])]
    result = {
        "flight_id": track.get("id"),
        "coordinates": len(coords),
        "timestamps": len(times),
        "altitudes": len(track.get("alt") or []),
        "ground_altitudes": len(track.get("ground_alt") or []),
        "engine_values": len(track.get("engine_sensor") or []),
        "start_utc": utc_iso(times[0]) if times else None,
        "end_utc": utc_iso(times[-1]) if times else None,
        "duration_seconds": times[-1] - times[0] if len(times) > 1 else 0,
        "minimum_interval_seconds": min(gaps) if gaps else None,
        "maximum_interval_seconds": max(gaps) if gaps else None,
        "mean_interval_seconds": sum(gaps) / len(gaps) if gaps else None,
    }
    print(json.dumps(result, indent=2))
    if args.csv:
        count = atomic_track_csv(Path(args.csv), track)
        print(f"Wrote {count} points to {args.csv}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect", help="Collect and resume the archive")
    collect_parser.add_argument("--output", default="weglide_archive_data")
    collect_parser.add_argument("--profile")
    collect_parser.add_argument("--start-date")
    collect_parser.add_argument("--stop-date", default="2015-01-01")
    collect_parser.add_argument("--max-flights", type=int, default=0)
    collect_parser.add_argument("--max-runtime-minutes", type=float, default=0,
                                help="Stop cleanly after this many minutes; 0 is unlimited")
    collect_parser.add_argument("--r2-sync-every-flights", type=int, default=0,
                                help="Publish an R2 checkpoint after this many new flights; 0 disables it")
    collect_parser.add_argument("--r2-prefix", default="north-america-v1")
    collect_parser.add_argument("--priority-area", choices=("na", "ne-us", "northeast"), default="northeast",
                                help="Download this takeoff region first within each day")
    collect_parser.add_argument("--restart-scan", action="store_true",
                                help="Reset this date-range checkpoint and rescan from --start-date")
    collect_parser.add_argument("--min-delay", type=float, default=0.5)
    collect_parser.add_argument("--max-delay", type=float, default=1.5)
    collect_parser.add_argument("--min-list-delay", type=float, default=2,
                                help="Minimum pause between listing requests (default 2)")
    collect_parser.add_argument("--max-list-delay", type=float, default=5,
                                help="Maximum pause between listing requests (default 5)")
    visibility = collect_parser.add_mutually_exclusive_group()
    visibility.add_argument("--headless", dest="headless", action="store_true")
    visibility.add_argument("--visible", dest="headless", action="store_false")
    collect_parser.set_defaults(headless=True)
    inspect_parser = sub.add_parser("inspect", help="Inspect/convert one saved track payload")
    inspect_parser.add_argument("file")
    inspect_parser.add_argument("--csv")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "inspect":
        return inspect(args)
    if args.min_delay < 0 or args.max_delay < args.min_delay:
        raise ValueError("Require 0 <= --min-delay <= --max-delay")
    if args.min_list_delay < 0 or args.max_list_delay < args.min_list_delay:
        raise ValueError("Require 0 <= --min-list-delay <= --max-list-delay")
    if args.max_flights < 0:
        raise ValueError("--max-flights must be nonnegative")
    if args.max_runtime_minutes < 0:
        raise ValueError("--max-runtime-minutes must be nonnegative")
    if args.r2_sync_every_flights < 0:
        raise ValueError("--r2-sync-every-flights must be nonnegative")
    return asyncio.run(collect(args))


if __name__ == "__main__":
    raise SystemExit(main())
