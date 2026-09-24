"""Independently audit saved payloads, CSV rows, and index; build a local review."""
import csv
from datetime import datetime, timezone
import gzip
import html
import io
import json
import math
import os
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'weglide_archive_data'
OUT = ROOT / 'validation'


def read_text(path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8', newline='') as f:
        return f.read()


def expected(values, i, n):
    if not values:
        return None
    position = i * (len(values) - 1) / max(n - 1, 1)
    lo, hi = math.floor(position), math.ceil(position)
    a, b = values[lo], values[hi]
    if a is None or b is None:
        return a if position - lo < .5 else b
    return a + (b - a) * (position - lo)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')


def main():
    OUT.mkdir(exist_ok=True)
    os.environ.setdefault('MPLCONFIGDIR', str(OUT / '.matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    OUT.mkdir(exist_ok=True)
    db = sqlite3.connect((DATA / 'index.sqlite3').as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
    records = [dict(r) for r in db.execute('select * from flights order by scoring_date desc, id desc')]
    db.close()
    exported = {int(r['id']): r for r in csv.DictReader((DATA / 'flights.csv').open(encoding='utf-8'))}
    assert set(exported) == {r['id'] for r in records}
    reports, cards = [], []
    samples = []
    for record in records:
        assert record['status'] == 'complete', record
        fid = record['id']
        paths = {key: DATA / record[key] for key in ('raw_track_path', 'detail_path', 'track_csv_path')}
        for key, path in paths.items():
            assert path.is_file() and path.suffix != '.part', path
            assert exported[fid][key] == record[key]
        track = json.loads(read_text(paths['raw_track_path']))
        detail = json.loads(read_text(paths['detail_path']))
        listing = json.loads(record['listing_json'])
        rows = list(csv.DictReader(io.StringIO(read_text(paths['track_csv_path']))))
        coords, times = track['geom']['coordinates'], track['time']
        assert track['id'] == detail['id'] == listing['id'] == fid
        assert track['geom']['type'] == 'LineString'
        assert len(rows) == len(coords) == record['point_count'] == int(exported[fid]['point_count'])
        assert all(b > a for a, b in zip(times, times[1:])), fid
        mapping = {'altitude_site_units': 'alt', 'ground_altitude_site_units': 'ground_alt',
                   'engine_sensor_raw': 'engine_sensor', 'fes_battery_raw': 'fes_battery',
                   'fes_energy_raw': 'fes_energy', 'fes_power_raw': 'fes_power'}
        for i, row in enumerate(rows):
            lon, lat = float(row['longitude']), float(row['latitude'])
            assert -180 <= lon <= 180 and -90 <= lat <= 90
            assert [lon, lat] == coords[i][:2]
            timestamp = expected(times, i, len(rows))
            assert math.isclose(float(row['unix_time']), timestamp, rel_tol=0, abs_tol=1e-6)
            assert abs(datetime.fromisoformat(row['timestamp_utc']).timestamp() - timestamp) <= 1e-6
            for column, key in mapping.items():
                value = expected(track.get(key), i, len(rows))
                assert row[column] == '' if value is None else math.isclose(float(row[column]), value, rel_tol=1e-10, abs_tol=1e-8), (fid, i, column)
            if row['altitude_site_units'] and row['ground_altitude_site_units']:
                assert math.isclose(float(row['agl_site_units']), float(row['altitude_site_units']) - float(row['ground_altitude_site_units']), abs_tol=1e-8)
        assert record['pilot'] == detail['user']['name']
        assert record['aircraft'] == detail['aircraft']['name']
        assert record['airport'] == detail['takeoff_airport']['name']
        for key in ('scoring_date', 'registration', 'competition_id'):
            assert record[key] == detail.get(key)
        assert record['start_utc'] == detail['takeoff_time']
        assert record['end_utc'] == detail['landing_time']
        assert times[0] <= datetime.fromisoformat(record['start_utc']).timestamp() <= datetime.fromisoformat(record['end_utc']).timestamp() <= times[-1]
        elapsed = [(float(row['unix_time']) - times[0]) / 60 for row in rows]
        alt = [float(row['altitude_site_units']) for row in rows]
        ground = [float(row['ground_altitude_site_units']) for row in rows]
        lat0, lon0 = coords[0][1], coords[0][0]
        east = [(c[0] - lon0) * 111.195 * math.cos(math.radians(lat0)) for c in coords]
        north = [(c[1] - lat0) * 111.195 for c in coords]
        fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), constrained_layout=True)
        axes[0].plot(east, north, color='#19748c', linewidth=.8)
        axes[0].scatter([east[0]], [north[0]], marker='o', s=50, color='#248047', label='First fix', zorder=3)
        axes[0].scatter([east[-1]], [north[-1]], marker='x', s=50, color='#c44a35', label='Last fix', zorder=4)
        axes[0].set(xlabel='East of first fix (approx. km)', ylabel='North of first fix (approx. km)', title='GPS route — local projection, no basemap', aspect='equal')
        axes[0].legend(fontsize=8)
        axes[1].plot(elapsed, alt, label='Altitude', color='#19748c', linewidth=1)
        axes[1].plot(elapsed, ground, label='Ground elevation', color='#9d7235', linewidth=1)
        axes[1].set(xlabel='Minutes since first fix', ylabel='Site altitude units (unverified)', title='Altitude and terrain')
        axes[1].legend(fontsize=8)
        for ax in axes:
            ax.grid(alpha=.2)
        fig.suptitle(f"{fid} | {record['pilot']} | {record['aircraft']}", fontsize=12)
        fig.savefig(OUT / f'{fid}.png', dpi=130)
        plt.close(fig)
        links = {}
        for key, source in paths.items():
            if source.suffix == '.gz':
                # Separate review copies avoid touching the existing archive or index.
                target = OUT / 'previous_flights' / source.parent.name / source.stem
                target.parent.mkdir(parents=True, exist_ok=True)
                text = read_text(source)
                if source.stem.endswith('.json'):
                    text = json.dumps(json.loads(text), indent=2, ensure_ascii=False) + '\n'
                target.write_text(text, encoding='utf-8')
                links[key] = target.relative_to(OUT).as_posix()
            else:
                links[key] = '../weglide_archive_data/' + source.relative_to(DATA).as_posix()
        report = {'id': fid, 'date': record['scoring_date'], 'pilot': record['pilot'], 'copilot': record['copilot'],
                  'aircraft': record['aircraft'], 'registration': record['registration'], 'airport': record['airport'],
                  'region': detail['takeoff_airport'].get('region'), 'points': len(rows), 'source_time_samples': len(times),
                  'interpolated': len(times) != len(rows), 'track_start_utc': iso(times[0]), 'track_end_utc': iso(times[-1]),
                  'takeoff_utc': record['start_utc'], 'landing_utc': record['end_utc'],
                  'duration_minutes': round((times[-1]-times[0])/60, 2),
                  'minimum_altitude': min(alt), 'maximum_altitude': max(alt),
                  'first_coordinate': coords[0], 'last_coordinate': coords[-1],
                  'bounds': [min(c[0] for c in coords), min(c[1] for c in coords), max(c[0] for c in coords), max(c[1] for c in coords)],
                  'new_plain_download': paths['raw_track_path'].suffix == '.json',
                  'downloaded_at': record['downloaded_at'], 'checks': 'PASS', 'files': links}
        reports.append(report)
        for index in sorted({0,1,2,len(rows)//2,len(rows)-2,len(rows)-1}):
            samples.append({'flight_id': fid, 'row_number': index + 1, **rows[index]})
        e = html.escape
        cards.append(f'''<section id="flight-{fid}"><h2>{fid} · {e(record['pilot'])}</h2>
        <p>{e(record['aircraft'])} · {e(record['registration'] or 'No registration')} · {e(record['airport'])} ({e(report['region'] or '')}) · {report['date']}</p>
        <p><strong>{len(rows):,} verified rows</strong> · {len(times):,} source time samples · {'Time/altitude interpolated by array position' if report['interpolated'] else 'Time/altitude arrays align with coordinates'}</p>
        <p>Track: {report['track_start_utc']} → {report['track_end_utc']}<br>Takeoff / landing: {e(record['start_utc'])} → {e(record['end_utc'])}</p>
        <img src="{fid}.png" alt="GPS route and altitude profile for flight {fid}">
        <p><a href="{links['track_csv_path']}">Full CSV</a> · <a href="{links['raw_track_path']}">Raw track JSON</a> · <a href="{links['detail_path']}">Detail JSON</a> · <a href="https://www.weglide.org/flight/{fid}">Compare on WeGlide</a></p></section>''')
    total = sum(r['points'] for r in reports)
    new = [r for r in reports if r['new_plain_download']]
    summary = {'generated_utc': datetime.now(timezone.utc).isoformat(), 'total_flights': len(reports),
               'total_rows': total, 'new_plain_flights': len(new), 'new_plain_rows': sum(r['points'] for r in new),
               'integrity': 'PASS', 'headless_live_validation': 'FAILED: browser fetch / CORS error',
               'visible_live_validation': 'PASS: one flight then five new flights, existing IDs skipped', 'flights': reports}
    (OUT / 'validation.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    with (OUT / 'sample_rows.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(samples[0]))
        writer.writeheader()
        writer.writerows(samples)
    navigation = ' · '.join(f'<a href="#flight-{r["id"]}">{r["id"]}</a>' for r in reports)
    (OUT / 'review.html').write_text(f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>WeGlide archive data review</title><style>body{{font:16px/1.6 system-ui,sans-serif;color:#223239;background:#fafbf9;max-width:1120px;margin:40px auto;padding:0 24px}}h1,h2{{line-height:1.2}}a{{color:#076786}}section{{padding:24px 0;border-top:1px solid #ccd5d5}}img{{width:100%;height:auto}}.status{{padding:18px;background:#e5f1e9}}code{{background:#eef1f2}}</style>
    <h1>WeGlide archive data review</h1><p class="status"><strong>{len(reports)} flights · {total:,} verified rows · all saved-data checks passed.</strong><br>{len(new)} flights were newly downloaded in visible Chrome. Five-flight resume and 5–10 second pacing passed. Headless requests failed; a long run has not started.</p>
    <p>Every CSV row was checked against its source coordinates and independently calculated time, altitude, terrain and sensor values. Flight IDs, metadata, file paths and exported index agree. This verifies faithful conversion and internal consistency, not independent GPS accuracy.</p>
    <ul><li>Some payloads have more coordinates than time/altitude samples. Intermediate values are linearly interpolated by array position; they are not original independent recorder fixes.</li><li>Track bounds include ground time; metadata start/end are takeoff/landing. Both are preserved.</li><li>Altitude units remain unverified. Missing engine/FES values stay blank. Negative AGL values are preserved rather than concealed.</li><li>These are public viewer tracks, not original IGC flight-recorder files. Suitable for route visualization and exploratory analysis; validate sampling/units before precise performance analysis.</li><li>Original compressed files and resume state are preserved; readable copies of older files are in this review folder.</li></ul>
    <p><a href="sample_rows.csv">Sample CSV rows</a> · <a href="validation.json">Full validation results</a></p><nav>{navigation}</nav>{''.join(cards)}</html>''', encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k != 'flights'}, indent=2))
    for r in reports:
        print(r['id'], r['pilot'], r['points'], r['region'], 'interpolated' if r['interpolated'] else 'aligned')


if __name__ == '__main__':
    main()
