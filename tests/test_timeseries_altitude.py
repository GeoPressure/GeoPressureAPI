"""Offline numeric and request-contract tests for the Earth Engine altitude expression."""
import ast
import datetime
import json
import math
from pathlib import Path
import types
import unittest

from altitude import ALTITUDE_FORMULAS, pressure_to_altitude

SOURCE = Path(__file__).resolve().parents[1] / "timeseries" / "timeseries.py"
TREE = ast.parse(SOURCE.read_text())
GLOBALS = {"math": math, "datetime": datetime, "json": json, "GEE_Service": object,
           "ALTITUDE_FORMULAS": ALTITUDE_FORMULAS, "pressure_to_altitude": pressure_to_altitude}
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
    def mod(self, x): return Number(self % x)
    def cos(self): return Number(math.cos(self))
    def abs(self): return Number(abs(self))
    def clamp(self, low, high): return Number(min(high, max(low, self)))
    def lt(self, x): return self < x
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


class Date:
    def __init__(self, timestamp):
        self.date = datetime.datetime.fromtimestamp(timestamp / 1000, datetime.timezone.utc)
    def getRelative(self, unit, period): return Number(self.date.timetuple().tm_yday - 1)


class Feature(dict):
    def __init__(self, properties, latitude):
        super().__init__(properties)
        self.latitude = latitude
    def geometry(self):
        return types.SimpleNamespace(coordinates=lambda: types.SimpleNamespace(
            getNumber=lambda index: Number(self.latitude)))


class TimeseriesAltitudeTest(unittest.TestCase):
    def test_production_expression_matches_independent_reference(self):
        for lat, day, expected in [(46.95, "2020-01-15", 1387.298550585463),
                                   (46.95, "2020-07-15", 1382.959077983491),
                                   (-33.87, "2020-07-15", 1385.927270830295),
                                   (90, "2020-01-15", 1388.800294606306),
                                   (-46, "2020-12-31", 1383.009395068535)]:
            processor = CLASS()
            processor.ee = types.SimpleNamespace(Image=Image, Number=Number, Date=Date,
                Algorithms=types.SimpleNamespace(If=lambda condition, yes, no: yes if condition else no))
            for formula, reference in [("virtual", expected), ("standard", 1379.358946332310)]:
                env = dict(GLOBALS, self=processor, coordinates=[7.45, lat],
                           dataset="single-levels", altitudeFormula=formula)
                exec(compile(ALTITUDE, str(SOURCE), "exec"), env)
                feature = Feature({"bestERA5": {"surface_pressure": 100000, "temperature_2m": 288,
                           "dewpoint_temperature_2m": 280}, "pressure": 90000,
                           "system:time_start": datetime.datetime.fromisoformat(day).replace(
                               tzinfo=datetime.timezone.utc).timestamp() * 1000}, lat)
                with self.subTest(lat=lat, day=day, formula=formula):
                    self.assertAlmostEqual(env["getAltitude"](feature), reference, places=9)
                    # pressurePath must call the same shared implementation with the same inputs.
                    path_source = SOURCE.parents[1] / "pressurePath" / "pressurePath.py"
                    path_tree = ast.parse(path_source.read_text())
                    path_altitude = next(n for n in ast.walk(path_tree)
                                         if isinstance(n, ast.FunctionDef) and n.name == "getAltitude")
                    dh_index = next(i for i, n in enumerate(path_altitude.body)
                                    if isinstance(n, ast.Assign) and n.targets[0].id == "dh")
                    path_altitude.body = path_altitude.body[:dh_index + 1] + [
                        ast.Return(value=ast.Name(id="dh", ctx=ast.Load()))]
                    path_module = ast.fix_missing_locations(ast.Module(body=[path_altitude], type_ignores=[]))
                    exec(compile(path_module, str(path_source), "exec"), env)
                    feature["bestERA5"]["geopotential"] = 500 * 9.80665
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
