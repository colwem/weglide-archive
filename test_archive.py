"""Offline regression tests for persistence and track normalization."""
import json
import argparse
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
import weglide_archive as archive


class ArchiveTests(unittest.TestCase):
    def track(self):
        return {'id': 1, 'geom': {'type': 'LineString', 'coordinates': [[-100, 30], [-99, 31], [-98, 32]]},
                'time': [1000, 1002, 1004], 'alt': [100, 200], 'ground_alt': [10], 'engine_sensor': []}

    def test_independent_array_lengths(self):
        points = list(archive.normalized_points(self.track()))
        self.assertEqual([p['altitude_site_units'] for p in points], [100, 150, 200])
        self.assertEqual([p['agl_site_units'] for p in points], [90, 140, 190])
        self.assertEqual([p['unix_time'] for p in points], [1000, 1002, 1004])

    def test_write_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = archive.open_database(root / 'index.sqlite3')
            try:
                archive.write_flight(db, root, {'id': 1, 'scoring_date': '2026-09-23'}, {'id': 1}, self.track())
                self.assertTrue(archive.completed_flight(db, 1))
                row = db.execute('select raw_track_path, track_csv_path, point_count from flights').fetchone()
                self.assertTrue(row[0].endswith('.json'))
                self.assertTrue(row[1].endswith('.csv'))
                self.assertEqual(row[2], 3)
                self.assertEqual(json.loads((root / row[0]).read_text()), self.track())
                self.assertEqual(len((root / row[1]).read_text().splitlines()), 4)
                self.assertFalse(list(root.rglob('*.part')))
            finally:
                db.close()

    def test_reject_mismatched_id_and_empty_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = archive.open_database(root / 'index.sqlite3')
            try:
                with self.assertRaises(ValueError):
                    archive.write_flight(db, root, {'id': 2}, {'id': 2}, self.track())
                with self.assertRaises(ValueError):
                    archive.write_flight(db, root, {'id': 1}, {'id': 1}, {'id': 1})
                self.assertEqual(db.execute('select count(*) from flights').fetchone()[0], 0)
            finally:
                db.close()

    def test_headless_defaults(self):
        args = archive.parser().parse_args(['collect'])
        self.assertTrue(args.headless)
        self.assertEqual((args.min_delay, args.max_delay), (5, 10))
        self.assertEqual(args.priority_area, 'northeast')
        self.assertFalse(archive.parser().parse_args(['collect', '--visible']).headless)

    def test_day_url_has_verified_filters(self):
        url = archive.day_url('https://api.weglide.org/v1/flight?continent_id_in=NA&junk=x', '2026-09-24', 100, 100)
        self.assertIn('continent_id_in=NA', url)
        self.assertIn('scoring_date_in=2026-09-24', url)
        self.assertIn('skip=100', url)
        self.assertIn('order_by=-scoring_date', url)
        self.assertNotIn('junk=', url)

    def test_northeast_priority_is_local_and_explicit(self):
        northeast = archive.area_regions('northeast')
        self.assertIn('US-NY', northeast)
        self.assertIn('CA-QC', northeast)
        self.assertTrue(archive.in_area({'takeoff_airport': {'region': 'US-VT'}}, northeast))
        self.assertFalse(archive.in_area({'takeoff_airport': {'region': 'US-TX'}}, northeast))

    def test_checkpoint_repeats_current_day_until_completed(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = archive.open_database(Path(temporary) / 'index.sqlite3')
            args = argparse.Namespace(area='na', stop_date='2026-09-20', start_date='2026-09-24', restart_scan=False)
            first = archive.checkpoint(db, args)
            self.assertEqual(first['current_date'], '2026-09-24')
            again = archive.checkpoint(db, args)
            self.assertEqual(again['current_date'], '2026-09-24')
            first['current_date'] = '2026-09-23'
            archive.save_checkpoint(db, first)
            self.assertEqual(archive.checkpoint(db, args)['current_date'], '2026-09-23')
            db.close()


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_and_network_failures_stop(self):
        for status in (0, 401, 403, 429):
            page = AsyncMock()
            page.evaluate.return_value = [{'status': status, 'body': 'test failure', 'retry_after': None}]
            with self.assertRaises(archive.StopAccess):
                await archive.browser_fetch(page, ['https://example.invalid'])

    async def test_successful_response_preserved(self):
        page = AsyncMock()
        page.evaluate.return_value = [{'status': 200, 'body': '{"id": 1}', 'retry_after': None}]
        result = await archive.browser_fetch(page, ['https://example.invalid'])
        self.assertEqual(result[0].body, '{"id": 1}')


if __name__ == '__main__':
    unittest.main()
