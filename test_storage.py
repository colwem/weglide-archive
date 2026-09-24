"""Offline tests for durable R2 publication order and object layout."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

import r2_storage
import weglide_archive as archive


class FakeS3:
    def __init__(self, fail_manifest=False):
        self.uploads = []
        self.objects = []
        self.fail_manifest = fail_manifest

    def upload_file(self, source, bucket, object_key, ExtraArgs=None):
        self.uploads.append((Path(source).name, bucket, object_key, ExtraArgs))

    def put_object(self, **kwargs):
        if self.fail_manifest:
            raise RuntimeError('manifest upload failed')
        self.objects.append(kwargs)


class StorageTests(unittest.TestCase):
    def test_object_key_normalization(self):
        self.assertEqual(r2_storage.key('/north-america-v1/', '/state/index.sqlite3'),
                         'north-america-v1/state/index.sqlite3')

    def test_push_publishes_index_after_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = archive.open_database(root / 'index.sqlite3')
            track = {'id': 1, 'geom': {'type': 'LineString', 'coordinates': [[-73, 42], [-72, 43]]},
                     'time': [1000, 1001], 'alt': [100, 110], 'ground_alt': [20, 21]}
            archive.write_flight(db, root, {'id': 1, 'scoring_date': '2026-09-23'}, {'id': 1}, track)
            archive.export_metadata(db, root / 'flights.csv')
            db.close()
            fake = FakeS3()
            r2_storage.push(fake, 'test-bucket', 'north-america-v1', root)
            keys = [item[2] for item in fake.uploads]
            self.assertEqual(keys[-1], 'north-america-v1/state/index.sqlite3')
            self.assertLess(keys.index('north-america-v1/data/raw_tracks/2026-09-23_1.json'),
                            keys.index('north-america-v1/state/index.sqlite3'))
            manifest = json.loads(fake.objects[0]['Body'])
            self.assertEqual(manifest['complete_flights'], 1)
            self.assertEqual(manifest['files_uploaded_this_run'], 3)

    def test_prune_happens_only_after_successful_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = archive.open_database(root / 'index.sqlite3')
            track = {'id': 1, 'geom': {'type': 'LineString', 'coordinates': [[-73, 42], [-72, 43]]},
                     'time': [1000, 1001], 'alt': [100, 110], 'ground_alt': [20, 21]}
            archive.write_flight(db, root, {'id': 1, 'scoring_date': '2026-09-23'}, {'id': 1}, track)
            archive.export_metadata(db, root / 'flights.csv')
            db.close()
            payloads = list(root.glob('*/*'))
            r2_storage.push(FakeS3(), 'test-bucket', 'north-america-v1', root, prune_uploaded=True)
            self.assertTrue(payloads)
            self.assertTrue(all(not path.exists() for path in payloads))
            self.assertTrue((root / 'index.sqlite3').exists())
            self.assertTrue((root / 'flights.csv').exists())

    def test_failed_manifest_keeps_staged_payloads_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = archive.open_database(root / 'index.sqlite3')
            track = {'id': 1, 'geom': {'type': 'LineString', 'coordinates': [[-73, 42], [-72, 43]]},
                     'time': [1000, 1001], 'alt': [100, 110], 'ground_alt': [20, 21]}
            archive.write_flight(db, root, {'id': 1, 'scoring_date': '2026-09-23'}, {'id': 1}, track)
            archive.export_metadata(db, root / 'flights.csv')
            db.close()
            payloads = list(root.glob('*/*'))
            with self.assertRaisesRegex(RuntimeError, 'manifest upload failed'):
                r2_storage.push(FakeS3(fail_manifest=True), 'test-bucket', 'north-america-v1',
                                root, prune_uploaded=True)
            self.assertTrue(all(path.exists() for path in payloads))


if __name__ == '__main__':
    unittest.main()
