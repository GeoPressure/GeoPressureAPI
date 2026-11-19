"""Shared Earth Engine altitude formula, matching GeoPressureR.

Justification: https://github.com/GeoPressure/altitude-validation
"""
import datetime
import gzip
import math
from pathlib import Path

ALTITUDE_FORMULAS = ("virtual", "standard")

# GPT3 lapse-rate harmonics (Landskron & Boehm 2018, https://doi.org/10.1007/s00190-017-1066-2):
# columns 18-22 (mean, annual cos/sin, semi-annual cos/sin of dT/dh, K/km) of
# https://vmf.geo.tuwien.ac.at/codes/gpt3_1.grd, one row per 1 degree cell, rows from 89.5 N and
# 0.5 E, longitude fastest. The same values GeoPressureR caches in gpt3_lapse_rate().
_GPT3_FILE = Path(__file__).with_name("gpt3_1_lapse_rate.csv.gz")
_GPT3 = None


def _gpt3_grid():
    global _GPT3
    if _GPT3 is None:
        with gzip.open(_GPT3_FILE, "rt") as f:
            next(f)
            _GPT3 = [tuple(float(v) / 1000 for v in line.split(",")) for line in f]
    return _GPT3


def gpt3_lapse_rate(lon, lat, time):
    """GPT3 lapse rate (K/m) at each position and UNIX time (s), as GeoPressureR.

    Bilinear between the four nearest 1 degree grid centres, nearest along latitude in the polar
    rows, and with the annual phase 2*pi*day_of_year/365.25 of the UTC date. GPT3 lapse rates are
    positive under inversions and can cross 0, where the barometric formula is 0/0, so values
    within 1e-5 K/m of 0 are set to -1e-5.
    """
    grid = _gpt3_grid()
    out = []
    for x, y, t in zip(lon, lat, time):
        i = min(max(89.5 - y, 0), 179)
        j = x % 360 - 0.5
        i0, j0 = math.floor(i), math.floor(j)
        di, dj = i - i0, j - j0
        i1, j1, j0 = min(i0 + 1, 179), (j0 + 1) % 360, j0 % 360
        doy = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).timetuple().tm_yday
        w = 2 * math.pi * doy / 365.25
        h = (1, math.cos(w), math.sin(w), math.cos(2 * w), math.sin(2 * w))

        def v(r, c):
            return sum(a * b for a, b in zip(grid[r * 360 + c], h))

        lapse = ((1 - di) * (1 - dj) * v(i0, j0) + di * (1 - dj) * v(i1, j0)
                 + (1 - di) * dj * v(i0, j1) + di * dj * v(i1, j1))
        out.append(-1e-5 if abs(lapse) < 1e-5 else lapse)
    return out


def era5_grid_snap(lon, lat, dataset):
    """Centre of the ERA5 cell holding a position, as GeoPressureR's era5_grid_snap().

    Earth Engine samples a point lying exactly on a cell edge from either neighbour, seemingly at
    random, and a GeoPressureR map at scale = 4 puts every position on a 0.25 degree edge. Sampling
    the cell centre makes the choice deterministic and the same as GeoPressureR's ARCO backend:
    nearest cell, longitude ties to the east, latitude ties to the north for single levels and to
    the south for ERA5-Land. Longitude is wrapped to [-180, 180).
    """
    land = dataset == "land"
    res = 0.1 if land else 0.25
    # round() removes floating-point noise so that exact ties follow the rule above
    lon = math.floor(round(lon / res + 0.5, 6)) * res
    lat = (math.ceil(round(lat / res - 0.5, 6)) if land else math.floor(round(lat / res + 0.5, 6))) * res
    return [(lon + 180) % 360 - 180, lat]


def pressure_to_altitude(ee, pressure, image, elevation, lapse, formula="virtual"):
    """Convert pressure (Pa) to altitude (m) using matched ERA5 fields.

    `lapse` is the GPT3 lapse rate (K/m) of the point, from gpt3_lapse_rate(); "standard" ignores
    it and uses -6.5 K/km with the 2 m temperature.
    """
    if formula == "virtual":
        td = image.select("dewpoint_temperature_2m").subtract(273.15)
        e = td.multiply(17.67).divide(td.add(243.5)).exp().multiply(611.2)
        temperature = image.expression(
            "T * (1 + 0.608 * 0.622 * e / (sp - 0.378 * e))",
            {"T": image.select("temperature_2m"), "sp": image.select("surface_pressure"), "e": e},
        )
        lapse = ee.Number(lapse)
    else:
        temperature = image.select("temperature_2m")
        lapse = ee.Number(-0.0065)
    return temperature.divide(lapse).multiply(
        ee.Image.constant(ee.Number(pressure)).divide(image.select("surface_pressure"))
        .pow(lapse.multiply(-8.31432 / 9.80665 / 0.0289644)).subtract(1)
    ).add(elevation)
