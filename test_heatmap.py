import unittest

import numpy as np
from shapely.geometry import LineString

from new_england_heatmap import add_track_contribution, build_geography, rasterize_line


class HeatmapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.geography = build_geography(5.0, 40.0)

    def test_grid_is_small_and_fixed_resolution(self):
        self.assertLess(self.geography["width"] * self.geography["height"], 30_000)
        self.assertEqual(self.geography["cell_m"], 5_000)

    def test_new_england_route_contributes_at_most_one_per_flight(self):
        heat = np.zeros((self.geography["height"], self.geography["width"]), dtype=np.float32)
        points = [(-72.8, 42.0), (-71.8, 42.5), (-70.8, 43.0)]
        self.assertTrue(add_track_contribution(heat, points, self.geography))
        self.assertAlmostEqual(float(heat.max()), 1.0)
        self.assertGreater(np.count_nonzero(heat), 0)

    def test_distant_route_is_rejected(self):
        heat = np.zeros((self.geography["height"], self.geography["width"]), dtype=np.float32)
        self.assertFalse(add_track_contribution(heat, [(-122.4, 37.7), (-121.9, 37.3)], self.geography))
        self.assertEqual(float(heat.sum()), 0.0)

    def test_rasterization_stays_in_grid(self):
        line = LineString([(self.geography["min_x"], self.geography["min_y"]),
                           (self.geography["max_x"], self.geography["max_y"])])
        rows, cols = rasterize_line(line, self.geography)
        self.assertTrue(np.all(rows >= 0))
        self.assertTrue(np.all(cols >= 0))
        self.assertTrue(np.all(rows < self.geography["height"]))
        self.assertTrue(np.all(cols < self.geography["width"]))


if __name__ == "__main__":
    unittest.main()
