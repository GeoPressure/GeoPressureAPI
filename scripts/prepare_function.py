"""Stage a standalone Cloud Function with the shared altitude module.

Usage: python scripts/prepare_function.py pressurePath /tmp/pressurePath-source
"""
from pathlib import Path
import shutil
import sys

root = Path(__file__).resolve().parents[1]
shutil.copytree(root / sys.argv[1], sys.argv[2],
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "gee-api-key.json", ".env"))
shutil.copy2(root / "altitude.py", sys.argv[2])
