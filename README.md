# WeGlide North America flight archive

Collect public viewer GPS tracks from the North America map, newest scoring
date first, with a SQLite resume index. Preserve raw track/detail payloads as
readable JSON, normalized CSV tracks, and a flights.csv metadata export.

## Verified local status — 2026-09-24

- One new flight followed by five more downloaded successfully with --visible.
- The second run skipped completed IDs and used 5–10 second inter-flight waits.
- All 11 archived flights (91,177 CSV rows) passed the independent audit.
- Six new plain-file flights contain 36,933 rows. Five older compressed flights
  remain untouched; readable review copies are under validation/previous_flights.
- **Headless fetches currently fail with browser CORS/network errors.** The
  visible browser succeeds. Headless remains the requested default, but use
  --visible for the currently verified workflow. Do not assume headless is ready.
- No unlimited archive run has started. Review validation/review.html first.

## Run a bounded collection

From this directory in PowerShell:

```powershell
.\.venv\Scripts\python.exe weglide_archive.py collect --max-flights 5 --visible
```

The collector scans one exact calendar day at a time. Its SQLite checkpoint
stores the current day, not a fragile page number. If a run stops partway through
September 23, it queries September 23 again, skips completed IDs, finishes every
page for that day, and only then advances to September 22.

Within each North American day, takeoffs in the northeastern US and eastern
Canada are downloaded first by default. This is a local priority because the
site did not honor a combined multi-region query during live verification. It
does not exclude the rest of North America. Use `--priority-area na` to retain
the site's order without Northeast priority.

The local .venv has Playwright installed. It was created using the desktop
app's bundled Python; recreating it with a normal installed Python is advisable
before relying on this computer for a long unattended run.

For a new environment:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

The collector prefers installed Chrome and falls back to bundled Chromium.
The persistent profile is beside the archive (weglide_archive_data_browser_profile).
Keep only one collector running against a profile/archive at a time.

## Data interpretation

- Raw JSON preserves all delivered fields, including original sampled arrays.
- CSV retains every supplied longitude/latitude pair. Arrays with different
  lengths are interpolated independently by normalized array position. This
  prevents indexing errors if a sensor array has a different sample count.
- Most validated viewer payloads have 3,000 time/altitude samples and a different
  number of coordinates. Derived rows are **not independent original IGC fixes**.
- start_utc and end_utc in metadata are site takeoff/landing times when available;
  the raw track and CSV can include ground time outside that interval.
- Altitude/ground/AGL remain labeled site_units until independently verified.
  Missing sensor values remain blank. Negative AGL values are not clipped.
- Flight ID, pilot, copilot when present, aircraft, registration, competition ID,
  airport, raw listing metadata, and download time are retained.
- Checks establish internal consistency and faithful conversion, not independent
  measurement accuracy or completeness of the entire WeGlide database.

## Inspect and audit

```powershell
.\.venv\Scripts\python.exe weglide_archive.py inspect PATH_TO_TRACK_JSON
.\.venv\Scripts\python.exe weglide_archive.py inspect PATH_TO_TRACK_JSON --csv converted.csv
.\.venv\Scripts\python.exe -m unittest -v test_archive.py
.\.venv\Scripts\python.exe -m pip install -r requirements-validation.txt
.\.venv\Scripts\python.exe validate_archive.py
```

Inspect accepts .json and older .json.gz. New CSV output is plain text.
The audit checks every CSV row against source arrays, IDs, metadata, index paths,
strictly increasing source timestamps, geographic ranges, and SQLite integrity.
It writes a browsable review with route and altitude plots, sample rows, and
machine-readable results. Old files/index entries are not altered by the audit.
Live-test status annotations describe this session, not a fresh network check
on every audit invocation.

## Collection options and future long run

```text
--output PATH        Archive directory (default weglide_archive_data)
--profile PATH       Persistent browser profile
--start-date DATE    Newest scoring date (default today)
--stop-date DATE     Oldest scoring date (default 2015-01-01)
--max-flights N      New-flight limit; 0 is unlimited
--max-runtime-minutes N  Stop cleanly after N minutes; 0 is unlimited
--priority-area AREA Northeast priority (default northeast), or na
--restart-scan       Reset the saved day and rescan from --start-date
--min-delay SECONDS  Minimum pause (default 5)
--max-delay SECONDS  Maximum pause (default 10)
--headless           Hide browser (default; currently fails locally)
--visible            Normal visible browser (validated)
```

Defaults average 7.5 seconds of waiting per attempted flight, plus network and
disk time, with 2–5 seconds between listing pages. Stop with Ctrl+C and rerun to
resume. The newest listings are rescanned, and completed flights are skipped.
HTTP 401/403/429 and browser-level fetch failures stop collection; three
consecutive other flight errors also stop it. A failed flight never becomes
complete in the index. Completed files are atomically renamed from .part.

Before a large run, confirm viewer-data sampling is suitable, resolve or accept
the visible-browser requirement, and assess disk space and actual pace.
Scope is public North American map flights, from 2015 onward by default;
it does not include private flights, all continents, or guaranteed original IGCs.

## GitHub-hosted smoke test

The public repository workflow runs one bounded Linux test with Chromium under
a virtual display. It verifies the resulting JSON, CSV, and SQLite index and
retains the small test artifact for three days. It does not run on a schedule and
does not hold the long-term archive. GitHub-hosted jobs have a six-hour ceiling,
so a production workflow must use bounded runs and durable external object
storage for data and checkpoint state.

The `Archive North America to R2` workflow restores only the SQLite checkpoint
and metadata export. Previously downloaded payloads stay in R2. New JSON and CSV
files are uploaded first and the updated SQLite index is published last, so a
failed upload cannot advertise incomplete data as complete. The default runtime
is 330 minutes, leaving 30 minutes for upload before GitHub's six-hour cutoff.

The existing archive contains old directories whose names end in .json or .csv
and which hold .part files. They are preserved and are not indexed as complete.
Do not mistake them for finished files.
