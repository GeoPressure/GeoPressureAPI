"""Offline numeric and request-contract tests for the Earth Engine altitude expression."""
import ast
import datetime
import json
import math
from pathlib import Path
import types
import unittest

from altitude import ALTITUDE_FORMULAS, gpt3_lapse_rate, pressure_to_altitude

SOURCE = Path(__file__).resolve().parents[1] / "timeseries" / "timeseries.py"
TREE = ast.parse(SOURCE.read_text())
GLOBALS = {"math": math, "datetime": datetime, "json": json, "GEE_Service": object,
           "ALTITUDE_FORMULAS": ALTITUDE_FORMULAS, "gpt3_lapse_rate": gpt3_lapse_rate,
           "pressure_to_altitude": pressure_to_altitude}
# Load endpoint definitions without importing/authenticating the Earth Engine service.
exec(compile(ast.Module(body=[n for n in TREE.body if not isinstance(n, (ast.Import, ast.ImportFrom))],
                        type_ignores=[]), str(SOURCE), "exec"), GLOBALS)
CLASS = GLOBALS["GP_timeseries_v2"]
ALTITUDE = next(n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef) and n.name == "getAltitude")
# Evaluate the production expression before sampling and CSV export.
ALTITUDE.body[-1] = ast.Return(value=ast.Name(id="dh", ctx=ast.Load()))
ALTITUDE = ast.fix_missing_locations(ast.Module(body=[ALTITUDE], type_ignores=[]))


class Number(float):
    def add(self, x): return Number(self + x)
    def subtract(self, x): return Number(self - x)
    def multiply(self, x): return Number(self * x)
    def divide(self, x): return Number(self / x)
    def pow(self, x): return Number(self ** x)
    def exp(self): return Number(math.exp(self))
    def rename(self, name): return self


class Image:
    def __new__(cls, bands):
        if isinstance(bands, str):
            if bands.startswith("projects/"):
                return Number(500)
            bands = {"geopotential": 500 * 9.80665}
        obj = super().__new__(cls)
        obj.bands = bands
        return obj

    @staticmethod
    def constant(x): return Number(x)
    def select(self, name): return Number(self.bands[name])
    def expression(self, expression, values):
        return Number(values["T"] * (1 + 0.608 * 0.622 * values["e"] /
                                     (values["sp"] - 0.378 * values["e"])))


# GeoPressureR 3.8.0 gpt3_lapse_rate() (K/m) and pressure_to_altitude(90000, 100000, 288, 500,
# dewpoint = 280, lon, lat, date) for an independent reference, including the poles, the
# antimeridian, the prime meridian, a cell edge and the last day of a leap year.
REFERENCE = [
    (7.45, 46.95, 1579046400, -0.0034646565581944559, 1386.797993495530363),
    (7.45, 46.95, 1594816200, -0.0066159279706807196, 1382.510324023624662),
    (-70.5, -33.87, 1594792800, -0.0057027454574127955, 1383.749960986652923),
    (179.9, 89.9, 1579046400, 0.0091284820378679469, 1404.212434311448533),
    (-179.9, -89.9, 1609459140, -0.0030817900266095566, 1387.320820747799871),
    (0, 0, 1577833200, -0.0090482820827621699, 1379.219738295664001),
    (24.375, -22.875, 1510862400, -0.0059258169408407191, 1383.446928945462105),
    (19.509, 48.58777, 1497918600, -0.007278330313732726, 1381.612571762198968),
    (151.2, -33.87, 1614556800, -0.0070121610810516613, 1381.973163596235281),
    (-0.3, 64.1, 1609416000, -0.0083353841211219037, 1380.182474450567724),
]
STANDARD = 1379.358946332309642


class TimeseriesAltitudeTest(unittest.TestCase):
    def test_gpt3_lapse_rate_matches_geopressurer(self):
        lon, lat, time, lapse, _ = zip(*REFERENCE)
        for got, expected, point in zip(gpt3_lapse_rate(lon, lat, time), lapse, REFERENCE):
            with self.subTest(point=point[:3]):
                self.assertAlmostEqual(got, expected, places=15)
        self.assertEqual(gpt3_lapse_rate([0], [0], [0]), gpt3_lapse_rate([360], [0], [0]))

    def test_production_expression_matches_independent_reference(self):
        # pressurePath must call the same shared implementation with the same inputs.
        path_source = SOURCE.parents[1] / "pressurePath" / "pressurePath.py"
        path_altitude = next(n for n in ast.walk(ast.parse(path_source.read_text()))
                             if isinstance(n, ast.FunctionDef) and n.name == "getAltitude")
        dh_index = next(i for i, n in enumerate(path_altitude.body)
                        if isinstance(n, ast.Assign) and n.targets[0].id == "dh")
        path_altitude.body = path_altitude.body[:dh_index + 1] + [
            ast.Return(value=ast.Name(id="dh", ctx=ast.Load()))]
        path_module = ast.fix_missing_locations(ast.Module(body=[path_altitude], type_ignores=[]))
        processor = CLASS()
        processor.ee = types.SimpleNamespace(Image=Image, Number=Number)
        for lon, lat, time, _, expected in REFERENCE:
            for formula, reference in [("virtual", expected), ("standard", STANDARD)]:
                env = dict(GLOBALS, self=processor, coordinates=[lon, lat],
                           dataset="single-levels", altitudeFormula=formula)
                feature = {"bestERA5": {"surface_pressure": 100000, "temperature_2m": 288,
                                        "dewpoint_temperature_2m": 280, "geopotential": 500 * 9.80665},
                           "pressure": 90000, "system:time_start": time * 1000,
                           "lapse": gpt3_lapse_rate([lon], [lat], [time])[0]}
                with self.subTest(point=(lon, lat, time), formula=formula):
                    exec(compile(ALTITUDE, str(SOURCE), "exec"), env)
                    self.assertAlmostEqual(env["getAltitude"](feature), reference, places=9)
                    exec(compile(path_module, str(path_source), "exec"), env)
                    self.assertAlmostEqual(env["getAltitude"](feature), reference, places=9)

    def test_request_defaults_overrides_and_validation(self):
        processor = CLASS()
        calls = []
        processor.explicitTimeCollection = lambda *args: calls.append(args) or "csv-url"
        processor.boundingTimeCollection = lambda *args: calls.append(args) or "csv-url"
        processor.checkPosition = lambda xy: (xy[0], xy[1], 1, True)
        request = {"lon": 7.45, "lat": 46.95, "time": [1579046400], "pressure": [90000]}
        result = processor.singleRequest(request, None)
        self.assertEqual(result[0], 200)
        self.assertEqual(calls[-1][-2:], ("single-levels", "virtual"))
        self.assertEqual(json.loads(result[2])["data"]["distInter"], 0)
        result = processor.singleRequest(dict(request, dataset="land", altitudeFormula="standard"), None)
        self.assertEqual(result[0], 200)
        self.assertEqual(calls[-1][-2:], ("land", "standard"))
        self.assertEqual(json.loads(result[2])["data"]["distInter"], 1)
        for override in [{"altitudeFormula": "invalid"}, {"dataset": "invalid"}]:
            self.assertEqual(processor.singleRequest(dict(request, **override), None)[0], 400)
        result = processor.singleRequest({"lon": 7.45, "lat": 46.95,
                                          "startTime": 1579046400, "endTime": 1579050000}, None)
        self.assertEqual(result[0], 200)
        self.assertEqual(calls[-1][-1], "single-levels")


if __name__ == "__main__":
    unittest.main()
