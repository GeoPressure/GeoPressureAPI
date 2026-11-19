"""era5_grid_snap() picks the same ERA5 cell as GeoPressureR's era5_grid_snap()."""
import unittest

from altitude import era5_grid_snap

# GeoPressureR 3.8.0 era5_grid_snap(lon, lat, dataset): ties on both 0.25 degree edges (as every
# position of a scale = 4 map), on 0.1 degree edges, near the antimeridian and the poles.
REFERENCE = [
    (24.375, -22.875, "single-levels", 24.5, -22.75),
    (-24.375, 22.875, "single-levels", -24.25, 23),
    (25.875, -19.125, "single-levels", 26, -19),
    (0.125, 0.125, "single-levels", 0.25, 0.25),
    (-0.125, -0.125, "single-levels", 0, 0),
    (179.9, 89.9, "single-levels", 180, 90),
    (180, -89.9, "single-levels", 180, -90),
    (-180, 0, "single-levels", -180, 0),
    (7.45, 46.95, "single-levels", 7.5, 47),
    (19.509, 48.58777, "single-levels", 19.5, 48.5),
    (24.95, -22.95, "single-levels", 25, -23),
    (-24.95, 22.95, "single-levels", -25, 23),
    (0.05, -0.05, "single-levels", 0, 0),
    (179.97, -0.05, "single-levels", 180, 0),
    (24.375, -22.875, "land", 24.4, -22.9),
    (-24.375, 22.875, "land", -24.4, 22.9),
    (25.875, -19.125, "land", 25.9, -19.1),
    (0.125, 0.125, "land", 0.1, 0.1),
    (-0.125, -0.125, "land", -0.1, -0.1),
    (179.9, 89.9, "land", 179.9, 89.9),
    (180, -89.9, "land", 180, -89.9),
    (-180, 0, "land", -180, 0),
    (7.45, 46.95, "land", 7.5, 46.9),
    (19.509, 48.58777, "land", 19.5, 48.6),
    (24.95, -22.95, "land", 25, -23),
    (-24.95, 22.95, "land", -24.9, 22.9),
    (0.05, -0.05, "land", 0.1, -0.1),
    (179.97, -0.05, "land", 180, -0.1),
]


class Era5GridSnapTest(unittest.TestCase):
    def test_matches_geopressurer(self):
        for lon, lat, dataset, lon_ref, lat_ref in REFERENCE:
            with self.subTest(lon=lon, lat=lat, dataset=dataset):
                lon_snap, lat_snap = era5_grid_snap(lon, lat, dataset)
                self.assertTrue(-180 <= lon_snap < 180)
                # Same cell: longitudes 180 and -180 are one meridian
                self.assertAlmostEqual((lon_snap - lon_ref + 180) % 360 - 180, 0, places=9)
                self.assertAlmostEqual(lat_snap, lat_ref, places=9)


if __name__ == "__main__":
    unittest.main()
