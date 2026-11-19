"""Each function folder is deployed on its own, so it carries its own copy of altitude.py."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AltitudeCopies(unittest.TestCase):
    def test_copies_match_root(self):
        source = (ROOT / "altitude.py").read_bytes()
        for folder in ("pressurePath", "timeseries"):
            with self.subTest(folder=folder):
                self.assertEqual(
                    (ROOT / folder / "altitude.py").read_bytes(),
                    source,
                    f"{folder}/altitude.py differs from altitude.py; copy the root file into {folder}/",
                )


if __name__ == "__main__":
    unittest.main()
