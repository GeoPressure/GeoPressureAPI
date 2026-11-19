"""Every endpoint reports which code serves it, from an identical copy of version.py."""
import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("map", "timeseries", "pressurePath", "elevationPath")

spec = importlib.util.spec_from_file_location("version", ROOT / "version.py")
version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version)

STARTED = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=datetime.timezone.utc)


class VersionCopies(unittest.TestCase):
    def test_copies_match_root(self):
        source = (ROOT / "version.py").read_bytes()
        for folder in FOLDERS:
            with self.subTest(folder=folder):
                self.assertEqual(
                    (ROOT / folder / "version.py").read_bytes(),
                    source,
                    f"{folder}/version.py differs from version.py; copy the root file into {folder}/",
                )

    def test_every_main_reports_the_version(self):
        for folder in FOLDERS:
            text = (ROOT / folder / "main.py").read_text()
            with self.subTest(folder=folder):
                self.assertIn("from version import VERSION_HEADER, add_version", text)
                self.assertIn('"X-GeoPressure-Version": VERSION_HEADER', text)
                self.assertIn("add_version(response)", text)


class DeploymentInfo(unittest.TestCase):
    def test_missing_information_is_null(self):
        info = version.deployment_info({}, ROOT / "map", STARTED)
        self.assertEqual(
            list(info),
            ["commit", "deployedAt", "service", "revision", "sourceHash", "startedAt"],
        )
        for key in ("commit", "deployedAt", "service", "revision"):
            self.assertIsNone(info[key])
        self.assertRegex(info["sourceHash"], r"^[0-9a-f]{12}$")
        self.assertEqual(info["startedAt"], "2026-11-19T12:00:00Z")

    def test_reads_environment(self):
        environ = {
            "GIT_COMMIT": "65a2119",
            "DEPLOYED_AT": "2026-11-19T10:00:00Z",
            "K_SERVICE": "timeseries",
            "K_REVISION": "timeseries-00042-abc",
        }
        info = version.deployment_info(environ, ROOT / "timeseries", STARTED)
        self.assertEqual(info["commit"], "65a2119")
        self.assertEqual(info["deployedAt"], "2026-11-19T10:00:00Z")
        self.assertEqual(info["service"], "timeseries")
        self.assertEqual(info["revision"], "timeseries-00042-abc")

    def test_empty_variable_is_null(self):
        self.assertIsNone(version.deployment_info({"GIT_COMMIT": ""}, ROOT / "map", STARTED)["commit"])

    def test_source_hash_follows_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "main.py").write_text("a = 1\n")
            (folder / "requirements.txt").write_text("ee\n")
            first = version.source_hash(folder)
            (folder / "notes.md").write_text("not source\n")
            self.assertEqual(version.source_hash(folder), first)
            (folder / "main.py").write_text("a = 2\n")
            self.assertNotEqual(version.source_hash(folder), first)

    def test_folders_have_different_hashes(self):
        hashes = {version.source_hash(ROOT / folder) for folder in FOLDERS}
        self.assertEqual(len(hashes), len(FOLDERS))

    def test_header_is_compact_json(self):
        self.assertEqual(json.loads(version.VERSION_HEADER), version.VERSION)
        self.assertNotIn("\n", version.VERSION_HEADER)


class AddVersion(unittest.TestCase):
    info = {"commit": None, "sourceHash": "abc"}

    def test_success_envelope(self):
        body = json.JSONEncoder().encode({"status": "success", "taskID": 1, "data": {"a": [1.5, None]}})
        out = version.add_version(body, self.info)
        self.assertTrue(out.startswith(body[:-1]), "the rest of the body is kept byte for byte")
        self.assertEqual(
            json.loads(out),
            {"status": "success", "taskID": 1, "data": {"a": [1.5, None]}, "version": self.info},
        )
        self.assertEqual(list(json.loads(out)), ["status", "taskID", "data", "version"])

    def test_error_envelope(self):
        body = json.JSONEncoder().encode(
            {"status": "error", "taskID": 1, "errorMessage": "x", "advice": "y"}
        )
        self.assertEqual(json.loads(version.add_version(body, self.info))["version"], self.info)

    def test_empty_object(self):
        self.assertEqual(json.loads(version.add_version("{}", self.info)), {"version": self.info})

    def test_other_bodies_unchanged(self):
        for body in ("", "Catastrophic Error", b'{"a": 1}', "[1, 2]", {"error": "x"}):
            self.assertIs(version.add_version(body, self.info), body)

    def test_default_uses_module_version(self):
        out = json.loads(version.add_version('{"status": "success"}'))
        self.assertEqual(out["version"], version.VERSION)


class MapMaskDirection(unittest.TestCase):
    """map.py needs Earth Engine to import; check the request key and band names in the source."""

    text = (ROOT / "map" / "map.py").read_text()

    def test_flag_and_bands(self):
        self.assertIn('"includeMaskDirection" in jsonObj.keys()', self.text)
        self.assertIn('outputBands += ["fracAbove", "fracBelow"]', self.text)
        self.assertIn('.rename(["mse", "probAlt", "fracAbove", "fracBelow"])', self.text)

    def test_default_path_unchanged(self):
        self.assertIn(
            'return error.multiply(error).addBands(isPossible).rename(["mse", "probAlt"])', self.text
        )
        self.assertIn("includeMaskDirection=False", self.text)


if __name__ == "__main__":
    unittest.main()
