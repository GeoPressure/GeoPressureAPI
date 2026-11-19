"""Every endpoint reports the code and data behind a response, from an identical provenance.py."""
import datetime
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("map", "timeseries", "pressurePath", "elevationPath")

spec = importlib.util.spec_from_file_location("provenance_root", ROOT / "provenance.py")
provenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provenance)

STARTED = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=datetime.timezone.utc)
SINGLE_END = 1762988400000  # 2025-11-12T23:00:00Z
LAND_END = 1762905600000  # 2025-11-12T00:00:00Z
HOURS = {"single-levels": SINGLE_END, "land": LAND_END}
MINMAX_FACTORS = [10, 2, 3, 4, 5, 6, 7, 7.5, 8, 9]
PERCENTILE_FACTORS = [1, 10, 2, 3, 4, 5, 6, 7, 7.5, 8, 9]

try:
    import numpy  # noqa: F401  (map.py needs it)

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


def load_endpoint(folder):
    """Import an endpoint module without Earth Engine: its EE calls are replaced in each test."""
    if "ee" not in sys.modules:
        try:
            import ee  # noqa: F401
        except ImportError:
            sys.modules["ee"] = types.ModuleType("ee")
    sys.path.insert(0, str(ROOT / folder))
    try:
        spec = importlib.util.spec_from_file_location(f"endpoint_{folder}", ROOT / folder / f"{folder}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def call(obj, body):
    code, _, response = obj.singleRequest(body, None)
    return code, json.loads(response)


class Copies(unittest.TestCase):
    def test_copies_match_root(self):
        source = (ROOT / "provenance.py").read_bytes()
        for folder in FOLDERS:
            with self.subTest(folder=folder):
                self.assertEqual(
                    (ROOT / folder / "provenance.py").read_bytes(),
                    source,
                    f"{folder}/provenance.py differs from provenance.py; copy the root file into {folder}/",
                )

    def test_every_main_sends_the_header(self):
        for folder in FOLDERS:
            text = (ROOT / folder / "main.py").read_text()
            with self.subTest(folder=folder):
                self.assertIn("from provenance import HEADER, HEADER_VALUE", text)
                self.assertIn("HEADER: HEADER_VALUE", text)

    def test_every_endpoint_appends_provenance(self):
        for folder in FOLDERS:
            text = (ROOT / folder / f"{folder}.py").read_text()
            with self.subTest(folder=folder):
                self.assertIn("return code, headers, add_provenance(body, groups)", text)

    def test_reported_assets_are_the_ones_used(self):
        used = {
            "map": [*provenance.ERA5_COLLECTION.values(), *provenance.ERA5_GEOPOTENTIAL.values()],
            "timeseries": [*provenance.ERA5_COLLECTION.values(), *provenance.ERA5_GEOPOTENTIAL.values()],
            "pressurePath": [*provenance.ERA5_COLLECTION.values(), *provenance.ERA5_GEOPOTENTIAL.values()],
            "elevationPath": [provenance.DEM_PERCENTILE],
        }
        used["map"].append(provenance.DEM_MINMAX)
        for folder, assets in used.items():
            text = (ROOT / folder / f"{folder}.py").read_text()
            for asset in assets:
                with self.subTest(folder=folder, asset=asset):
                    self.assertIn(f'"{asset}"', text, f"{folder} no longer uses {asset}; update provenance.py")


class CodeGroup(unittest.TestCase):
    def test_missing_information_is_null(self):
        code = provenance.code_group({}, ROOT / "map", STARTED)
        self.assertEqual(list(code), ["commit", "deployedAt", "service", "revision", "sourceHash", "startedAt"])
        for key in ("commit", "deployedAt", "service", "revision"):
            self.assertIsNone(code[key])
        self.assertRegex(code["sourceHash"], r"^[0-9a-f]{12}$")
        self.assertEqual(code["startedAt"], "2026-11-19T12:00:00Z")

    def test_reads_environment(self):
        environ = {"GIT_COMMIT": "65a2119", "DEPLOYED_AT": "2026-11-19T10:00:00Z",
                   "K_SERVICE": "timeseries", "K_REVISION": "timeseries-00042-abc"}
        code = provenance.code_group(environ, ROOT / "timeseries", STARTED)
        self.assertEqual([code[k] for k in ("commit", "deployedAt", "service", "revision")], list(environ.values()))

    def test_empty_variable_is_null(self):
        self.assertIsNone(provenance.code_group({"GIT_COMMIT": ""}, ROOT / "map", STARTED)["commit"])

    def test_source_hash_follows_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "main.py").write_text("a = 1\n")
            (folder / "requirements.txt").write_text("ee\n")
            first = provenance.source_hash(folder)
            (folder / "notes.md").write_text("not source\n")
            self.assertEqual(provenance.source_hash(folder), first)
            (folder / "main.py").write_text("a = 2\n")
            self.assertNotEqual(provenance.source_hash(folder), first)

    def test_folders_have_different_hashes(self):
        self.assertEqual(len({provenance.source_hash(ROOT / f) for f in FOLDERS}), len(FOLDERS))

    def test_header_is_the_code_group_as_compact_json(self):
        self.assertEqual(provenance.HEADER, "X-GeoPressure-Provenance")
        self.assertEqual(json.loads(provenance.HEADER_VALUE), {"code": provenance.CODE})
        self.assertNotIn(" ", provenance.HEADER_VALUE.replace(provenance.CODE["startedAt"], ""))


class DataGroups(unittest.TestCase):
    def test_dataset_used(self):
        self.assertEqual(provenance.dataset_used("Single-Levels"), "single-levels")
        self.assertEqual(provenance.dataset_used("land"), "land")
        for other in ("both", "foo", None):
            self.assertEqual(provenance.dataset_used(other), "both")

    def test_era5_group(self):
        self.assertEqual(
            provenance.era5_group("single-levels", SINGLE_END, "centre"),
            {"dataset": "single-levels", "collections": ["ECMWF/ERA5/HOURLY"],
             "geopotential": ["ECMWF/ERA5/HOURLY/20200101T00"],
             "lastHour": "2025-11-12T23:00:00Z", "cell": "centre"},
        )
        both = provenance.era5_group("both", None, "nearest")
        self.assertEqual(both["collections"], ["ECMWF/ERA5_LAND/HOURLY", "ECMWF/ERA5/HOURLY"])
        self.assertEqual(both["geopotential"][0], provenance.ERA5_GEOPOTENTIAL["land"])
        self.assertIsNone(both["lastHour"])

    def test_last_hour(self):
        self.assertEqual(provenance.last_hour(HOURS, "single-levels"), SINGLE_END)
        self.assertEqual(provenance.last_hour(HOURS, "land"), LAND_END)
        self.assertEqual(provenance.last_hour(HOURS, "both"), LAND_END)
        self.assertIsNone(provenance.last_hour({"single-levels": SINGLE_END, "land": None}, "both"))

    def test_altitude_group(self):
        self.assertEqual(provenance.altitude_group("virtual"), {"formula": "virtual", "lapseRate": "gpt3_1"})
        self.assertEqual(provenance.altitude_group("standard"), {"formula": "standard", "lapseRate": "-6.5 K/km"})

    def test_dem_factor_matches_the_endpoint_rule(self):
        # filter(factor >= min(scale, max factor)).sort("factor").first()
        self.assertEqual(provenance.dem_factor(MINMAX_FACTORS, 4), 4)
        self.assertEqual(provenance.dem_factor(MINMAX_FACTORS, 1), 2)
        self.assertEqual(provenance.dem_factor(MINMAX_FACTORS, 7.2), 7.5)
        self.assertEqual(provenance.dem_factor(MINMAX_FACTORS, 50), 10)
        self.assertEqual(provenance.dem_factor(PERCENTILE_FACTORS, 1), 1)
        self.assertIsNone(provenance.dem_factor(None, 4))

    def test_iso_utc(self):
        self.assertEqual(provenance.iso_utc(0), "1970-01-01T00:00:00Z")
        self.assertIsNone(provenance.iso_utc(None))


class LastHoursCache(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.reads = []
        self.answers = [dict(HOURS)]

    def reader(self):
        self.reads.append(self.now)
        return self.answers[min(len(self.reads), len(self.answers)) - 1]

    def cache(self):
        return provenance.Era5LastHours(self.reader, clock=lambda: self.now)

    def test_read_at_start_and_after_six_hours(self):
        cache = self.cache()
        self.answers.append({"single-levels": SINGLE_END + 86400000, "land": LAND_END + 86400000})
        self.now = 6 * 3600
        self.assertEqual(cache.get("single-levels"), SINGLE_END)
        self.assertEqual(len(self.reads), 1)
        self.now = 6 * 3600 + 1
        self.assertEqual(cache.get("single-levels"), SINGLE_END + 86400000)
        self.assertEqual(cache.get("both"), LAND_END + 86400000)
        self.assertEqual(len(self.reads), 2)

    def test_unknown_is_retried_and_a_failed_read_keeps_the_old_value(self):
        self.answers = [{"single-levels": None, "land": LAND_END}, {"single-levels": SINGLE_END, "land": None}]
        cache = self.cache()
        self.assertIsNone(cache.get("single-levels"))
        self.now = 61
        self.assertEqual(cache.get("single-levels"), SINGLE_END)
        self.assertEqual(cache.get("land"), LAND_END)

    def test_416_message_is_the_map_message(self):
        code, headers, body = provenance.era5_unavailable(7, "single-levels", SINGLE_END)
        self.assertEqual(code, 416)
        self.assertEqual(json.loads(body), {
            "status": "error", "taskID": 7, "lastERA5": SINGLE_END,
            "errorMessage": "ERA5 single-levels data not available from 2025-11-12 23:00:00. "
                            "Request only pressure with earlier date."})


class Envelope(unittest.TestCase):
    groups = {"era5": {"dataset": "land"}, "dem": {"asset": "x", "factor": 4}}

    def test_success_envelope(self):
        body = json.JSONEncoder().encode({"status": "success", "taskID": 1, "data": {"a": [1.5, None]}})
        out = provenance.add_provenance(body, self.groups)
        self.assertTrue(out.startswith(body[:-1]), "the rest of the body is kept byte for byte")
        parsed = json.loads(out)
        self.assertEqual(list(parsed), ["status", "taskID", "data", "provenance"])
        self.assertEqual(list(parsed["provenance"]), ["code", "era5", "dem"])

    def test_groups_in_fixed_order_and_unused_left_out(self):
        out = provenance.provenance({"dem": {}, "altitude": {}, "era5": {}})
        self.assertEqual(list(out), ["code", "era5", "altitude", "dem"])
        self.assertEqual(list(provenance.provenance({"era5": None})), ["code"])
        with self.assertRaises(ValueError):
            provenance.provenance({"version": {}})

    def test_empty_object(self):
        self.assertEqual(json.loads(provenance.add_provenance("{}")), {"provenance": {"code": provenance.CODE}})

    def test_other_bodies_unchanged(self):
        for body in ("", "Catastrophic Error", b'{"a": 1}', "[1, 2]", {"error": "x"}):
            self.assertIs(provenance.add_provenance(body), body)


@unittest.skipUnless(HAS_NUMPY, "map.py needs numpy")
class MapEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module = load_endpoint("map")
        obj = module.GP_map_v2.__new__(module.GP_map_v2)
        obj.lastHours = provenance.Era5LastHours(lambda: dict(HOURS))
        obj.demFactors = MINMAX_FACTORS
        obj.getMSE_Map = lambda *args, **kwargs: {"format": "GEOTIFF"}
        cls.obj, cls.module = obj, module

    body = {"W": -18, "S": 4, "E": 16, "N": 51, "time": [1572075000], "pressure": [97766], "label": [1]}

    def test_success(self):
        code, out = call(self.obj, self.body)
        self.assertEqual(code, 200)
        prov = out["provenance"]
        self.assertEqual(list(prov), ["code", "era5", "altitude", "dem"])
        self.assertEqual(prov["era5"]["dataset"], "single-levels")
        self.assertEqual(prov["era5"]["lastHour"], "2025-11-12T23:00:00Z")
        self.assertEqual(prov["era5"]["cell"], "centre")
        self.assertEqual(prov["altitude"], {"formula": "standard", "lapseRate": "-6.5 K/km"})
        self.assertEqual(prov["dem"], {"asset": provenance.DEM_MINMAX, "factor": 4})

    def test_cell_shift_picks_the_era5_grid_snap_cell(self):
        # A pixel centre on the corner of four cells, moved against the shift, must land in the
        # cell era5_grid_snap() picks: the sign of (snap - centre) is the opposite of the shift.
        sys.path.insert(0, str(ROOT / "timeseries"))
        try:
            from altitude import era5_grid_snap
        finally:
            sys.path.pop(0)
        module = self.module
        corners = {"single-levels": (6.125, 46.875), "both": (6.125, 46.875), "land": (6.05, 45.95)}
        for dataset, (lon, lat) in corners.items():
            dlon, dlat = module.era5CellShift(dataset)
            snap = era5_grid_snap(lon, lat, dataset)
            with self.subTest(dataset=dataset):
                self.assertLess(abs(dlon), 1e-5)
                self.assertEqual((snap[0] > lon, snap[1] > lat), (dlon < 0, dlat < 0))

    def test_same_request_same_provenance(self):
        later = dict(self.body, time=[1572078600])
        self.assertEqual(call(self.obj, self.body)[1]["provenance"], call(self.obj, later)[1]["provenance"])

    def test_416_carries_the_last_hour(self):
        code, out = call(self.obj, dict(self.body, time=[SINGLE_END // 1000 + 3600], dataset="land"))
        self.assertEqual(code, 416)
        self.assertEqual(out["provenance"]["era5"]["lastHour"], "2025-11-12T00:00:00Z")

    def test_validation_error_has_only_code(self):
        code, out = call(self.obj, {"W": 1})
        self.assertEqual(code, 400)
        self.assertEqual(list(out["provenance"]), ["code"])


class TimeseriesEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module = load_endpoint("timeseries")
        obj = module.GP_timeseries_v2.__new__(module.GP_timeseries_v2)
        obj.lastHours = provenance.Era5LastHours(lambda: dict(HOURS))
        obj.explicitTimeCollection = lambda *args: "https://url"
        obj.boundingTimeCollection = lambda *args: "https://url"
        obj.checkPosition = lambda coordinates: (*coordinates, 0, False)
        cls.obj = obj

    def test_pressure_mode_reports_altitude(self):
        code, out = call(self.obj, {"lon": 6, "lat": 46, "time": [1], "pressure": [97766]})
        self.assertEqual(code, 200)
        prov = out["provenance"]
        self.assertEqual(list(prov), ["code", "era5", "altitude"])
        self.assertEqual(prov["era5"]["collections"], ["ECMWF/ERA5/HOURLY"])
        self.assertEqual(prov["era5"]["cell"], "centre")
        self.assertEqual(prov["altitude"], {"formula": "virtual", "lapseRate": "gpt3_1"})

    def test_time_range_mode_has_no_altitude(self):
        _, out = call(self.obj, {"lon": 6, "lat": 46, "startTime": 1, "endTime": 2, "dataset": "land"})
        self.assertEqual(list(out["provenance"]), ["code", "era5"])
        self.assertEqual(out["provenance"]["era5"]["lastHour"], "2025-11-12T00:00:00Z")

    def test_validation_error_has_only_code(self):
        code, out = call(self.obj, {"lon": 6, "lat": 46, "dataset": "both"})
        self.assertEqual(code, 400)
        self.assertEqual(list(out["provenance"]), ["code"])

    def test_416_past_the_end_of_era5(self):
        end = SINGLE_END // 1000
        self.assertEqual(call(self.obj, {"lon": 6, "lat": 46, "time": [end - 3600, end], "pressure": [1, 1]})[0], 200)
        code, out = call(self.obj, {"lon": 6, "lat": 46, "time": [end, end + 3600], "pressure": [1, 1]})
        self.assertEqual(code, 416)
        self.assertEqual(out["lastERA5"], SINGLE_END)
        self.assertEqual(out["provenance"]["era5"]["lastHour"], "2025-11-12T23:00:00Z")
        self.assertEqual(call(self.obj, {"lon": 6, "lat": 46, "startTime": end - 7200, "endTime": end})[0], 200)
        self.assertEqual(call(self.obj, {"lon": 6, "lat": 46, "startTime": end - 7200, "endTime": end + 3600})[0], 416)
        code, out = call(self.obj, {"lon": 6, "lat": 46, "startTime": 1, "endTime": end, "dataset": "land"})
        self.assertEqual((code, out["lastERA5"]), (416, LAND_END))


class PressurePathEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module = load_endpoint("pressurePath")
        obj = module.GP_pressurePath.__new__(module.GP_pressurePath)
        obj.lastHours = provenance.Era5LastHours(lambda: dict(HOURS))
        obj.getPressureAlongPath = lambda *args: {"time": [1]}
        cls.obj = obj

    def test_both_with_pressure(self):
        body = {"lon": [6], "lat": [46], "time": [1], "variable": ["surface_pressure"],
                "pressure": [97766], "dataset": "both", "altitudeFormula": "standard"}
        code, out = call(self.obj, body)
        self.assertEqual(code, 200)
        self.assertEqual(list(out), ["status", "taskID", "data", "warning", "provenance"])
        era5 = out["provenance"]["era5"]
        self.assertEqual(era5["dataset"], "both")
        self.assertEqual(era5["collections"], ["ECMWF/ERA5_LAND/HOURLY", "ECMWF/ERA5/HOURLY"])
        self.assertEqual(era5["lastHour"], "2025-11-12T00:00:00Z")
        self.assertEqual(out["provenance"]["altitude"]["formula"], "standard")

    def test_without_pressure_has_no_altitude(self):
        _, out = call(self.obj, {"lon": [6], "lat": [46], "time": [1], "variable": ["temperature_2m"]})
        self.assertEqual(list(out["provenance"]), ["code", "era5"])

    def test_unknown_last_hour_is_null_and_not_checked(self):
        known = self.obj.lastHours
        self.obj.lastHours = provenance.Era5LastHours(lambda: {"land": LAND_END})
        try:
            code, out = call(self.obj, {"lon": [6], "lat": [46], "time": [SINGLE_END // 1000 + 86400],
                                        "variable": ["temperature_2m"]})
        finally:
            self.obj.lastHours = known
        self.assertEqual(code, 200)
        self.assertIsNone(out["provenance"]["era5"]["lastHour"])

    def test_416_past_the_end_of_era5(self):
        between = LAND_END // 1000 + 3600  # after the end of ERA5-Land, before that of single levels
        body = {"lon": [6, 6], "lat": [46, 46], "time": [1, between], "variable": ["temperature_2m"]}
        self.assertEqual(call(self.obj, body)[0], 200)
        code, out = call(self.obj, dict(body, dataset="both"))
        self.assertEqual((code, out["lastERA5"]), (416, LAND_END))
        self.assertEqual(out["errorMessage"],
                         "ERA5 both data not available from 2025-11-12 00:00:00. Request only pressure with earlier date.")
        self.assertEqual(list(out["provenance"]), ["code", "era5"])


class ElevationPathEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module = load_endpoint("elevationPath")
        obj = module.GP_elevationPath.__new__(module.GP_elevationPath)
        obj.demFactors = PERCENTILE_FACTORS
        obj.getElevationAlongPath = lambda *args: {"percentileData": {}}
        cls.obj = obj

    def test_dem_only(self):
        code, out = call(self.obj, {"lon": [8, 9], "lat": [48, 44], "scale": 20, "samplingScale": 1})
        self.assertEqual(code, 200)
        self.assertEqual(out["provenance"]["dem"], {"asset": provenance.DEM_PERCENTILE, "factor": 10})
        self.assertEqual(list(out["provenance"]), ["code", "dem"])

    def test_no_debug_getinfo(self):
        self.assertNotIn("print(elevation_image.getInfo())", (ROOT / "elevationPath" / "elevationPath.py").read_text())


class MapMaskSplit(unittest.TestCase):
    """The split needs Earth Engine to run; check the request key and band names in the source."""

    text = (ROOT / "map" / "map.py").read_text()

    def test_flag_and_bands(self):
        self.assertIn('"includeMaskSplit" in jsonObj.keys()', self.text)
        self.assertIn('outputBands += ["probAbove", "probBelow"]', self.text)
        self.assertIn('.rename(["mse", "probAlt", "probAbove", "probBelow"])', self.text)

    def test_default_path_unchanged(self):
        self.assertIn('return error.multiply(error).addBands(isPossible).rename(["mse", "probAlt"])', self.text)
        self.assertIn("includeMaskSplit=False", self.text)


if __name__ == "__main__":
    unittest.main()
