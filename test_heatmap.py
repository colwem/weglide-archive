import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from shapely.geometry import LineString

from new_england_heatmap import (
    BOUNDARY_SOURCE,
    add_track_contribution,
    build_geography,
    load_state_geometries,
    rasterize_line,
)
from export_new_england_tracks import identity_from_key, qualifying_polyline, write_bundle


class HeatmapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.geography = build_geography(5.0, 40.0)

    def test_grid_is_small_and_fixed_resolution(self):
        self.assertLess(self.geography["width"] * self.geography["height"], 30_000)
        self.assertEqual(self.geography["cell_m"], 5_000)

    def test_official_boundaries_include_every_new_england_state(self):
        self.assertEqual(set(load_state_geometries()), {"CT", "RI", "MA", "VT", "NH", "ME"})
        self.assertIn("Census Bureau", BOUNDARY_SOURCE)

    def test_new_england_route_contributes_at_most_one_per_flight(self):
        heat = np.zeros((self.geography["height"], self.geography["width"]), dtype=np.float32)
        crossings = np.zeros_like(heat, dtype=np.uint32)
        points = [(-72.8, 42.0), (-71.8, 42.5), (-70.8, 43.0)]
        self.assertTrue(add_track_contribution(heat, crossings, points, self.geography))
        self.assertAlmostEqual(float(heat.max()), 1.0)
        self.assertGreater(np.count_nonzero(heat), 0)
        self.assertEqual(int(crossings.max()), 1)
        self.assertGreater(np.count_nonzero(crossings), 0)

    def test_distant_route_is_rejected(self):
        heat = np.zeros((self.geography["height"], self.geography["width"]), dtype=np.float32)
        crossings = np.zeros_like(heat, dtype=np.uint32)
        self.assertFalse(add_track_contribution(
            heat, crossings, [(-122.4, 37.7), (-121.9, 37.3)], self.geography))
        self.assertEqual(float(heat.sum()), 0.0)
        self.assertEqual(int(crossings.sum()), 0)

    def test_rasterization_stays_in_grid(self):
        line = LineString([(self.geography["min_x"], self.geography["min_y"]),
                           (self.geography["max_x"], self.geography["max_y"])])
        rows, cols = rasterize_line(line, self.geography)
        self.assertTrue(np.all(rows >= 0))
        self.assertTrue(np.all(cols >= 0))
        self.assertTrue(np.all(rows < self.geography["height"]))
        self.assertTrue(np.all(cols < self.geography["width"]))

    def test_export_identity_comes_from_storage_key(self):
        self.assertEqual(identity_from_key(
            "north-america-v1/data/raw_tracks/2024-06-12_12345.json.gz"),
            ("2024-06-12", 12345))

    def test_export_keeps_intersecting_track(self):
        result = qualifying_polyline([(-72.7, 42.0), (-72.6, 43.0)], self.geography, 250)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(len(result), 2)

    def test_export_metadata_uses_sorted_iso_date_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tracks.npz"
            coordinates = np.asarray([[-72.0, 42.0], [-71.0, 43.0]], dtype=np.float32)
            metadata = write_bundle(path, [
                ("2026-09-30", 2, coordinates),
                ("2018-04-12", 1, coordinates),
            ], 2, 0, 250.0, 1.0)
            self.assertEqual((metadata["date_start"], metadata["date_end"]),
                             ("2018-04-12", "2026-09-30"))
            self.assertEqual(json.loads(path.with_suffix(".json").read_text())["date_start"],
                             "2018-04-12")


if __name__ == "__main__":
    unittest.main()
