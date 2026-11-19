"""Each function folder is deployed on its own, so it carries its own copy of altitude.py and
the GPT3 grid it reads."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AltitudeCopies(unittest.TestCase):
    def test_copies_match_root(self):
        for name in ("altitude.py", "gpt3_1_lapse_rate.csv.gz"):
            source = (ROOT / name).read_bytes()
            for folder in ("pressurePath", "timeseries"):
                with self.subTest(folder=folder, file=name):
                    self.assertEqual(
                        (ROOT / folder / name).read_bytes(),
                        source,
                        f"{folder}/{name} differs from {name}; copy the root file into {folder}/",
                    )


if __name__ == "__main__":
    unittest.main()
