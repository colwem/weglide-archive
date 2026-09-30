"""Offline regression tests for persistence and track normalization."""
import json
import argparse
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
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
        self.assertEqual((args.min_delay, args.max_delay), (4, 8))
        self.assertEqual((args.min_list_delay, args.max_list_delay), (2, 5))
        self.assertEqual(args.collection_plan, 'new-england-first')
        self.assertEqual(args.max_runtime_minutes, 0)
        self.assertEqual(args.r2_sync_every_flights, 0)
        self.assertFalse(archive.parser().parse_args(['collect', '--visible']).headless)

    def test_day_url_has_verified_filters(self):
        url = archive.day_url('https://api.weglide.org/v1/flight?continent_id_in=NA&junk=x', '2026-09-24', 100, 100)
        self.assertIn('continent_id_in=NA', url)
        self.assertIn('scoring_date_in=2026-09-24', url)
        self.assertIn('skip=100', url)
        self.assertIn('order_by=-scoring_date', url)
        self.assertNotIn('junk=', url)

    def test_new_england_is_exactly_the_six_state_region(self):
        self.assertEqual(set(archive.NEW_ENGLAND_REGIONS),
                         {'US-CT', 'US-RI', 'US-MA', 'US-VT', 'US-NH', 'US-ME'})
        vermont = {'takeoff_airport': {'region': 'US-VT'}}
        new_york = {'takeoff_airport': {'region': 'US-NY'}}
        self.assertTrue(archive.in_collection_phase(vermont, 'new-england'))
        self.assertFalse(archive.in_collection_phase(new_york, 'new-england'))
        self.assertFalse(archive.in_collection_phase(vermont, 'rest-na'))
        self.assertTrue(archive.in_collection_phase(new_york, 'rest-na'))
        self.assertEqual(archive.collection_phases('new-england-first'),
                         ('new-england', 'rest-na'))

    def test_checkpoint_repeats_current_day_until_completed(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = archive.open_database(Path(temporary) / 'index.sqlite3')
            args = argparse.Namespace(collection_phase='new-england', stop_date='2026-09-20',
                                      start_date='2026-09-24', restart_scan=False)
            first = archive.checkpoint(db, args)
            self.assertEqual(first['current_date'], '2026-09-24')
            again = archive.checkpoint(db, args)
            self.assertEqual(again['current_date'], '2026-09-24')
            first['current_date'] = '2026-09-23'
            archive.save_checkpoint(db, first)
            self.assertEqual(archive.checkpoint(db, args)['current_date'], '2026-09-23')
            rest_args = argparse.Namespace(collection_phase='rest-na', stop_date='2026-09-20',
                                           start_date='2026-09-24', restart_scan=False)
            self.assertEqual(archive.checkpoint(db, rest_args)['current_date'], '2026-09-24')
            self.assertNotEqual(first['scope'], archive.checkpoint(db, rest_args)['scope'])
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


class CollectionPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_rest_of_na_cannot_start_until_new_england_is_finished(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(output=temporary, collection_plan='new-england-first',
                                      max_runtime_minutes=0)
            phase_runner = AsyncMock(return_value=0)
            with patch.object(archive, 'collect_phase', phase_runner), \
                    patch.object(archive, 'phase_finished', return_value=False):
                self.assertEqual(await archive.collect(args), 0)
            self.assertEqual(phase_runner.await_count, 1)
            self.assertEqual(phase_runner.await_args.args[0].collection_phase, 'new-england')

    async def test_rest_of_na_starts_after_new_england_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(output=temporary, collection_plan='new-england-first',
                                      max_runtime_minutes=0)
            phase_runner = AsyncMock(return_value=0)
            with patch.object(archive, 'collect_phase', phase_runner), \
                    patch.object(archive, 'phase_finished', return_value=True):
                self.assertEqual(await archive.collect(args), 0)
            phases = [call.args[0].collection_phase for call in phase_runner.await_args_list]
            self.assertEqual(phases, ['new-england', 'rest-na'])


if __name__ == '__main__':
    unittest.main()
