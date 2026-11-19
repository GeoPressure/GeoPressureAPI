"""Identify the code that serves a response.

Every function folder carries an identical copy of this file (see tests/test_version.py), and its
main.py appends VERSION to each JSON response and sends it in the X-GeoPressure-Version header.

- commit, deployedAt: set by the deployer through the GIT_COMMIT and DEPLOYED_AT environment
  variables; null when not set.
- service, revision: K_SERVICE and K_REVISION, set by Cloud Run and Cloud Functions; null elsewhere.
- sourceHash: SHA-256 (first 12 hex digits) of the *.py, requirements.txt and *.csv.gz files of the
  function folder, computed when the instance starts. Needs nothing from the deployer. To check
  which code a service runs, compute it from a checkout with `python3 version.py <folder>`.
- startedAt: when this instance loaded the code (UTC). A long-lived instance keeps state from its
  start, such as the last available ERA5 hour.
"""

import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

SOURCE_PATTERNS = ("*.py", "requirements.txt", "*.csv.gz")


def source_hash(folder):
    """Hash the source files of a function folder, independently of file dates and order."""
    folder = Path(folder)
    paths = sorted({path for pattern in SOURCE_PATTERNS for path in folder.glob(pattern)})
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def deployment_info(environ=None, folder=None, started_at=None):
    """Describe the deployed code. Missing information is None (null in JSON)."""
    environ = os.environ if environ is None else environ
    folder = Path(__file__).resolve().parent if folder is None else folder
    if started_at is None:
        started_at = datetime.datetime.now(datetime.timezone.utc)

    def env(name):
        return environ.get(name) or None

    return {
        "commit": env("GIT_COMMIT"),
        "deployedAt": env("DEPLOYED_AT"),
        "service": env("K_SERVICE"),
        "revision": env("K_REVISION"),
        "sourceHash": source_hash(folder),
        "startedAt": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


VERSION = deployment_info()
VERSION_HEADER = json.dumps(VERSION, separators=(",", ":"))


def add_version(body, version=None):
    """Append a "version" field to a JSON object response.

    The rest of the body is kept byte for byte. Anything that is not a JSON object string is
    returned unchanged.
    """
    version = VERSION if version is None else version
    if not isinstance(body, str):
        return body
    stripped = body.rstrip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return body
    field = '"version": ' + json.dumps(version)
    if stripped[1:-1].strip() == "":
        return "{" + field + "}"
    return stripped[:-1] + ", " + field + "}"


if __name__ == "__main__":
    for folder in sys.argv[1:] or ["."]:
        print(folder, source_hash(folder))
