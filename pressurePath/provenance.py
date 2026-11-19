"""Describe the code and the data that serve a response.

Every function folder carries an identical copy of this file (see tests/test_provenance.py). Each
endpoint appends a "provenance" object to every JSON response, errors included, and main.py sends
its "code" group in the X-GeoPressure-Provenance header of every response.

The object has up to four groups. An endpoint leaves out the groups it does not use:

- code: which code serves the response.
  - commit, deployedAt: GIT_COMMIT and DEPLOYED_AT, set by the deployer; null when not set.
  - service, revision: K_SERVICE and K_REVISION, set by Cloud Run and Cloud Functions.
  - sourceHash: SHA-256 (first 12 hex digits) of the *.py, requirements.txt and *.csv.gz files of
    the function folder. Compute it from a checkout with `python3 provenance.py <folder>`.
  - startedAt: when this instance loaded the code.
- era5: dataset, collections and geopotential (first takes precedence), lastHour (last ERA5 hour
  available, read once when the instance starts) and cell (how a position picks its ERA5 cell).
- altitude: formula and lapseRate of the barometric formula.
- dem: asset and factor (pixels per degree) of the DEM layer used.

No value changes from one request to the next on the same instance, so that clients can collapse
the objects of many requests. Unknown values are null, which means the same as a missing key.
"""

import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

ERA5_COLLECTION = {"single-levels": "ECMWF/ERA5/HOURLY", "land": "ECMWF/ERA5_LAND/HOURLY"}
ERA5_GEOPOTENTIAL = {
    "single-levels": "ECMWF/ERA5/HOURLY/20200101T00",
    "land": "projects/earthimages4unil/assets/PostDocProjects/rafnuss/Geopot_ERA5",
}
DEM_MINMAX = "projects/earthimages4unil/assets/PostDocProjects/rafnuss/GLO_minMax_reduced"
DEM_PERCENTILE = "projects/earthimages4unil/assets/PostDocProjects/rafnuss/GLO_reduced"
LAPSE_RATE = {"virtual": "gpt3_1", "standard": "-6.5 K/km"}

SOURCE_PATTERNS = ("*.py", "requirements.txt", "*.csv.gz")
GROUPS = ("code", "era5", "altitude", "dem")
HEADER = "X-GeoPressure-Provenance"


def source_hash(folder):
    """Hash the source files of a function folder, independently of file dates and order."""
    folder = Path(folder)
    paths = sorted({path for pattern in SOURCE_PATTERNS for path in folder.glob(pattern)})
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def iso_utc(value):
    """ISO 8601 UTC string from a datetime or UNIX milliseconds; None stays None."""
    if value is None:
        return None
    if not isinstance(value, datetime.datetime):
        value = datetime.datetime.fromtimestamp(value / 1000, datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def code_group(environ=None, folder=None, started_at=None):
    """Describe the deployed code."""
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
        "startedAt": iso_utc(started_at),
    }


CODE = code_group()
HEADER_VALUE = json.dumps({"code": CODE}, separators=(",", ":"))


def dataset_used(dataset):
    """The ERA5 dataset an endpoint queries: anything else than "single-levels" or "land" is
    served from the joined collections, as "both"."""
    dataset = str(dataset).lower()
    return dataset if dataset in ERA5_COLLECTION else "both"


def era5_group(dataset, last_hour, cell):
    """ERA5 group; last_hour in UNIX milliseconds (or None), cell "centre" or "nearest"."""
    used = dataset_used(dataset)
    order = ["land", "single-levels"] if used == "both" else [used]
    return {
        "dataset": used,
        "collections": [ERA5_COLLECTION[name] for name in order],
        "geopotential": [ERA5_GEOPOTENTIAL[name] for name in order],
        "lastHour": iso_utc(last_hour),
        "cell": cell,
    }


def altitude_group(formula):
    return {"formula": formula, "lapseRate": LAPSE_RATE.get(formula)}


def dem_factor(factors, scale):
    """Factor of the DEM layer an endpoint picks for a scale: the finest layer when the scale is
    finer than all of them, otherwise the coarsest layer at least as fine as the scale."""
    if not factors:
        return None
    target = min(float(scale), max(factors))
    return min(factor for factor in factors if factor >= target)


def dem_group(asset, factors, scale):
    return {"asset": asset, "factor": dem_factor(factors, scale)}


def era5_last_hours(ee):
    """Last available hour (UNIX ms) of each ERA5 collection, or None if the query fails."""
    hours = {}
    for name, collection in ERA5_COLLECTION.items():
        try:
            hours[name] = (
                ee.ImageCollection(collection)
                .filterDate("2024", "2100")
                .aggregate_max("system:time_start")
                .getInfo()
            )
        except Exception as e:
            print(f"Could not read the last hour of {collection}: {e}")
            hours[name] = None
    return hours


def last_hour(hours, dataset):
    """Last hour of the dataset queried; "both" ends with the earlier of the two collections."""
    used = dataset_used(dataset)
    values = list(hours.values()) if used == "both" else [hours.get(used)]
    return None if None in values else min(values)


def dem_factors(ee, asset):
    """Factors of the layers of a DEM collection, or None if the query fails."""
    try:
        return ee.ImageCollection(asset).aggregate_array("factor").getInfo()
    except Exception as e:
        print(f"Could not read the factors of {asset}: {e}")
        return None


def provenance(groups=None):
    """The provenance object: the code group, then the data groups an endpoint filled in."""
    groups = groups or {}
    unknown = set(groups) - set(GROUPS)
    if unknown:
        raise ValueError(f"Unknown provenance groups: {sorted(unknown)}")
    out = {"code": CODE}
    for name in GROUPS[1:]:
        if groups.get(name) is not None:
            out[name] = groups[name]
    return out


def add_provenance(body, groups=None):
    """Append a "provenance" field to a JSON object response.

    The rest of the body is kept byte for byte. Anything that is not a JSON object string is
    returned unchanged.
    """
    if not isinstance(body, str):
        return body
    stripped = body.rstrip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return body
    field = '"provenance": ' + json.dumps(provenance(groups))
    if stripped[1:-1].strip() == "":
        return "{" + field + "}"
    return stripped[:-1] + ", " + field + "}"


if __name__ == "__main__":
    for folder in sys.argv[1:] or ["."]:
        print(folder, source_hash(folder))
