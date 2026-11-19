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
  available, cached per instance and re-read every 6 h, see Era5LastHours) and cell (how a
  position picks its ERA5 cell).
- altitude: formula and lapseRate of the barometric formula.
- dem: asset and factor (pixels per degree) of the DEM layer used.

No value changes from one request to the next on the same instance (except lastHour when it is
re-read), so that clients can collapse the objects of many requests. Unknown values are null,
which means the same as a missing key.

This module also holds the ERA5 availability check shared by the endpoints: requests past the
last available hour are answered with HTTP 416 (era5_unavailable).
"""

import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ERA5_COLLECTION = {"single-levels": "ECMWF/ERA5/HOURLY", "land": "ECMWF/ERA5_LAND/HOURLY"}
ERA5_GEOPOTENTIAL = {
    "single-levels": "ECMWF/ERA5/HOURLY/20200101T00",
    "land": "projects/earthimages4unil/assets/PostDocProjects/rafnuss/Geopot_ERA5",
}
DEM_MINMAX = "projects/earthimages4unil/assets/PostDocProjects/rafnuss/GLO_minMax_reduced"
DEM_PERCENTILE = "projects/earthimages4unil/assets/PostDocProjects/rafnuss/GLO_reduced"
LAPSE_RATE = {"virtual": "gpt3_1", "standard": "-6.5 K/km"}

LAST_HOUR_MAX_AGE = 6 * 3600  # s; ERA5 grows by a day at a time, a few days behind real time
LAST_HOUR_RETRY = 60  # s; retry sooner while a collection's last hour is unknown

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
    """ERA5 group; last_hour in UNIX milliseconds (or None). cell names the rule by which a
    position picks its ERA5 cell: "centre" is era5_grid_snap(), used by every endpoint."""
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


def startup_info(ee, dem_asset=None):
    """Last available hour (UNIX ms) of each ERA5 collection and, if dem_asset is given, the
    factors of the layers of that DEM collection, read in a single Earth Engine call.

    Returns (hours, factors): hours maps each dataset to its last hour, factors is a list or None.
    If the call fails, every value is None.
    """
    queries = [
        ee.ImageCollection(collection).filterDate("2024", "2100").aggregate_max("system:time_start")
        for collection in ERA5_COLLECTION.values()
    ]
    if dem_asset is not None:
        queries.append(ee.ImageCollection(dem_asset).aggregate_array("factor"))
    try:
        values = ee.List(queries).getInfo()
    except Exception as e:
        print(f"Could not read the last ERA5 hours or the DEM factors: {e}")
        values = [None] * len(queries)
    hours = dict(zip(ERA5_COLLECTION, values))
    return hours, (values[len(ERA5_COLLECTION)] if dem_asset is not None else None)


def era5_last_hours(ee):
    """Last available hour (UNIX ms) of each ERA5 collection, or None if the query fails."""
    return startup_info(ee)[0]


def last_hour(hours, dataset):
    """Last hour of the dataset queried; "both" ends with the earlier of the two collections."""
    used = dataset_used(dataset)
    values = list(hours.values()) if used == "both" else [hours.get(used)]
    return None if None in values else min(values)


class Era5LastHours:
    """Last available hour (UNIX ms) of each ERA5 collection, cached per instance.

    Read when the instance starts, then again on the first request more than max_age seconds
    later, so that a warm instance does not reject hours ERA5 has gained since. While a value is
    unknown (the query failed), it is retried after `retry` seconds; a failed re-read keeps the
    previous value.
    """

    def __init__(self, reader, max_age=LAST_HOUR_MAX_AGE, retry=LAST_HOUR_RETRY, clock=time.time,
                 initial=None):
        """initial: hours already read (e.g. by startup_info()), used instead of a first read."""
        self.reader, self.max_age, self.retry, self.clock = reader, max_age, retry, clock
        self.hours = {name: None for name in ERA5_COLLECTION}
        if initial is None:
            self.refresh()
        else:
            self.hours.update({name: initial.get(name) for name in ERA5_COLLECTION})
            self.read_at = self.clock()

    def refresh(self):
        new = self.reader()
        self.hours = {
            name: new.get(name) if new.get(name) is not None else self.hours.get(name)
            for name in ERA5_COLLECTION
        }
        self.read_at = self.clock()

    def get(self, dataset):
        """Last hour of the dataset queried (UNIX ms), or None if unknown."""
        age = self.clock() - self.read_at
        if age > self.max_age or (None in self.hours.values() and age > self.retry):
            self.refresh()
        return last_hour(self.hours, dataset)


def era5_unavailable(task_id, dataset, last_hour_ms):
    """HTTP 416 response for a request past the last available ERA5 hour."""
    last = datetime.datetime.fromtimestamp(last_hour_ms / 1000, datetime.timezone.utc)
    return (
        416,
        {"Content-type": "application/json"},
        json.JSONEncoder().encode(
            {
                "status": "error",
                "taskID": task_id,
                "errorMessage": "ERA5 {} data not available from {}. Request only pressure with earlier date.".format(
                    dataset, last.replace(tzinfo=None)
                ),
                "lastERA5": last_hour_ms,
            }
        ),
    )


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
