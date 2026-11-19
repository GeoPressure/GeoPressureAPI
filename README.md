# GeoPressureAPI

[![Test Server](https://github.com/GeoPressure/GeoPressureAPI/actions/workflows/test-server.yml/badge.svg)](https://github.com/GeoPressure/GeoPressureAPI/actions/workflows/test-server.yml)

GeoPressureAPI is a JSON API that compares geolocator pressure timeseries with atmospheric pressure from [ERA5](https://developers.google.com/earth-engine/datasets/catalog/ECMWF_ERA5_HOURLY) and [ERA5-Land](https://developers.google.com/earth-engine/datasets/catalog/ECMWF_ERA5_LAND_HOURLY) reanalysis, computed on Google Earth Engine. It is the backend of [GeoPressureR](https://github.com/GeoPressure/GeoPressureR).

Please [file an issue](https://github.com/GeoPressure/GeoPressureAPI/issues/new) if you find anything missing or wrong.

## Endpoints

| Endpoint | Description |
| -------- | ----------- |
| [`/map/`](#1-pressure-map) | Pressure mismatch maps (GeoTIFF) from geolocator data |
| [`/timeseries/`](#2-pressure-timeseries) | ERA5 pressure timeseries at one location |
| [`/elevationPath/`](#3-ground-elevation-along-a-path) | Ground elevation profile along a polyline |
| [`/pressurePath/`](#4-pressure-data-along-a-path) | ERA5 variables and altitude along a path |

### Conventions

- **Base URL:** `https://glp.mgravey.com/GeoPressure/v2/`. Endpoint names are case-sensitive.
- **Requests:** `POST` with a JSON body (`Content-Type: application/json`). The base URL answers with a `307` redirect to the service, so clients must follow redirects (e.g. `curl -L`).
- **Units:** time as [UNIX timestamps](https://en.wikipedia.org/wiki/Unix_time) in seconds, pressure in Pa, coordinates in WGS84 degrees, distances and altitudes in m.
- **Time matching:** each timestamp is paired with the closest ERA5 hour within ±1 h.
- **Grid cell:** `/pressurePath/` and `/timeseries/` sample each position, and `/map/` each pixel centre, at the centre of the ERA5 cell holding it: the nearest grid point, with ties going east, and north (single levels and `"both"`) or south (ERA5-Land). GeoPressureR's ARCO backend uses the same rule. A point exactly on a cell edge, such as every position or pixel centre of a GeoPressureR map at `scale = 4`, would otherwise be sampled from either neighbour: at random for a point, and from the cell to the south-east (single levels) or north-east (ERA5-Land) for a map pixel.
- **Success:** `{"status": "success", "taskID": ..., "data": {...}, "provenance": {...}}`. The fields documented below are inside `data`.
- **Error:** HTTP `400` with `{"status": "error", "taskID": ..., "errorMessage": ..., "advice": ..., "provenance": {...}}`.
- **Past the end of ERA5:** `/map/`, `/timeseries/` and `/pressurePath/` answer HTTP `416` with `{"status": "error", "taskID": ..., "errorMessage": ..., "lastERA5": ..., "provenance": {...}}` when a requested time is after the last available hour of the selected `dataset` (`lastERA5`, UNIX ms; with `"both"`, the earlier end of the two collections). Each service instance reads these last hours when it starts and again every 6 h, so it does not reject hours ERA5 has gained since.
- **Provenance:** every JSON response ends with a `provenance` object describing the code and the data that served it (see [Provenance](#provenance)). Its `code` group is also sent, as compact JSON `{"code": {...}}`, in the `X-GeoPressure-Provenance` header of every response, including the plain-text ones.

### Provenance

`provenance` has the same groups on every endpoint. An endpoint leaves out the groups it does not use, and a validation error carries only `code`. No value changes from one request to the next on the same instance, so the objects of many requests can be collapsed into the distinct ones. Unknown values are `null`, which means the same as a missing key. Timestamps are ISO 8601 UTC.

| Group | Field | Description |
| ----- | ----- | ----------- |
| `code` | `commit` | Git commit the deployment was built from (`GIT_COMMIT`, set at deploy) |
| | `deployedAt` | Deploy time (`DEPLOYED_AT`, set at deploy) |
| | `service`, `revision` | Cloud Run service and revision (`K_SERVICE`, `K_REVISION`) |
| | `sourceHash` | First 12 hex digits of the SHA-256 of the function folder's `*.py`, `requirements.txt` and `*.csv.gz` files; always present |
| | `startedAt` | When the serving instance started |
| `era5` | `dataset` | `"single-levels"`, `"land"` or `"both"`, as queried |
| | `collections` | Earth Engine ERA5 collections; with `"both"`, ERA5-Land first, as it takes precedence |
| | `geopotential` | Ground level of the barometric formula, in the same order |
| | `lastHour` | Last ERA5 hour available, as used for the `416` check (see Past the end of ERA5) |
| | `cell` | `"centre"`: each position or pixel centre takes the ERA5 cell holding it, by the rule of Grid cell |
| `altitude` | `formula` | `"virtual"` or `"standard"` (see [Altitude formula](#altitude-formula)); the map mask always uses `"standard"` |
| | `lapseRate` | `"gpt3_1"` (GPT3 grid) or `"-6.5 K/km"` |
| `dem` | `asset` | Earth Engine DEM collection |
| | `factor` | Resolution of the DEM layer used (pixels per degree) |

| Endpoint | Groups |
| -------- | ------ |
| `/map/` | `code`, `era5`, `altitude`, `dem` |
| `/timeseries/` | `code`, `era5`, and `altitude` with `time` + `pressure` |
| `/pressurePath/` | `code`, `era5`, and `altitude` with `pressure` |
| `/elevationPath/` | `code`, `dem` |

`sourceHash` needs nothing from the deployer: to check which code an endpoint serves, run `python3 provenance.py map timeseries pressurePath elevationPath` in a checkout and compare. Each endpoint has its own hash.

```json
"provenance": {
  "code": {
    "commit": "65a2119...",
    "deployedAt": "2026-11-19T10:00:00Z",
    "service": "geopressureapi-timeseries",
    "revision": "geopressureapi-timeseries-00042-abc",
    "sourceHash": "3f2a9c1b7d4e",
    "startedAt": "2026-11-19T10:05:12Z"
  },
  "era5": {
    "dataset": "single-levels",
    "collections": ["ECMWF/ERA5/HOURLY"],
    "geopotential": ["ECMWF/ERA5/HOURLY/20200101T00"],
    "lastHour": "2026-11-12T23:00:00Z",
    "cell": "centre"
  },
  "altitude": { "formula": "virtual", "lapseRate": "gpt3_1" }
}
```

---

## 1. Pressure Map

`POST https://glp.mgravey.com/GeoPressure/v2/map/`

Computes, for each `label` (e.g. a stationary period), a map of pressure mismatch between the geolocator and ERA5. At most `maxSample` measurements are used per label, so long periods cost no more than short ones.

### Request

| Parameter | Type | Required | Default | Description |
| --------- | ---- | -------- | ------- | ----------- |
| `W`, `S`, `E`, `N` | `number` | ✅ | | Bounding box (degrees) |
| `time` | `number[]` | ✅ | | UNIX timestamps |
| `pressure` | `number[]` | ✅ | | Geolocator pressure (Pa) |
| `label` | `(string\|number)[]` | ✅ | | Grouping label of each measurement; one map per unique label |
| `scale` | `number` | | `4` | Pixels per degree (4 = 0.25°, 10 = 0.1°). `(E-W)*scale` and `(N-S)*scale` must be integers |
| `maxSample` | `number` | | `250` | Maximum measurements randomly sampled per label (`max_sample` also accepted) |
| `margin` | `number` | | `30` | Altitude tolerance of the mask (m; 1 hPa ≈ 10 m) |
| `includeMask` | `boolean` | | `true` | Return the mask band |
| `maskThreshold` | `number` | | `0.9` | MSE pixels whose mask is below this value are set to `-1`; `0` disables |
| `landDensityThreshold` | `number` | | `0` | Minimum land fraction (0–1) of an ERA5 pixel to be kept |
| `dataset` | `string` | | `"single-levels"` | `"single-levels"`, `"land"` or `"both"` (see [§4](#4-pressure-data-along-a-path)) |
| `includeMaskSplit` | `boolean` | | `false` | Add the `probAbove` and `probBelow` bands (see below) |

### Response

| Field | Type | Description |
| ----- | ---- | ----------- |
| `format` | `string` | `"GEOTIFF"` |
| `labels` | `(string\|number)[]` | Unique labels, in the same order as `urls` |
| `urls` | `(string\|null)[]` | GeoTIFF download URL per label (`null` on failure) |
| `errors` | `(string\|null)[]` | Reason for each `null` URL |
| `resolution` | `number` | Pixel size (degrees) |
| `size` | `number[]` | `[width, height]` in pixels |
| `bbox` | `object` | `{W, S, E, N}` |
| `time2GetUrls` | `number` | Time spent generating URLs (s) |
| `includeMask`, `maskThreshold` | | Values applied |
| `includeMaskSplit`, `bands` | | Only with `includeMaskSplit = true`: `true`, and the band names of each GeoTIFF in order |

If `time` extends past the last available ERA5 hour of the selected `dataset`, the request fails with HTTP `416` and a `lastERA5` field (ms) (see [Conventions](#conventions)).

Each GeoTIFF contains:

- **Band 1 – MSE**: [mean square error](https://en.wikipedia.org/wiki/Mean_squared_error) between geolocator and ERA5 surface pressure, after removing the mean of each (so an unknown constant altitude offset does not matter).
- **Band 2 – Mask** _(if `includeMask`)_: proportion of measurements whose altitude, from the [barometric formula](https://en.wikipedia.org/wiki/Barometric_formula), falls within the ground elevation range of the pixel (Copernicus GLO DEM min/max) ± `margin`.
- **Bands 3–4 (or 2–3 without the mask) – `probAbove`, `probBelow`** _(if `includeMaskSplit`)_: proportion of measurements whose altitude is above the ground range + `margin` (the bird flew, perched high or climbed during the label) and below the ground range − `margin` (sensor offset, or a valley lower than the pixel's ground). On land, mask + `probAbove` + `probBelow` = 1.
- **Special values:** `-1` below `maskThreshold` (MSE band only), `-2` water / no data (all bands).

> [!NOTE]
> **Change of November 2026: ERA5 cell of a map pixel.** Every pixel centre of a map at `scale = 4` lies on the corner of four ERA5 cells. Until then, `/map/` took the cell to the south-east of it, while `/timeseries/`, `/pressurePath/` and GeoPressureR's ARCO backend take the cell to the north-east (see Grid cell). `/map/` now takes the same cell, for the MSE and for the mask (surface pressure, temperature and orography). Maps at `scale = 4` therefore move by one ERA5 row (0.25°) in latitude, and results computed before and after differ slightly. Maps at scales whose pixel centres are not on cell edges (e.g. `scale = 10` with single levels) are unchanged. ERA5-Land maps at `scale = 10` take the cell to the south-east instead of the north-east. A response made after the change has `provenance.era5.cell = "centre"`; one made before has no `provenance`.

With `includeMaskSplit` unset or `false`, the response and the GeoTIFFs are unchanged.

### Example

```http
POST https://glp.mgravey.com/GeoPressure/v2/map/
Content-Type: application/json

{
  "W": -18, "S": 4, "E": 16, "N": 51,
  "time": [1572075000, 1572076800, 1572078600],
  "pressure": [97766, 97800, 97833],
  "label": [1, 1, 1]
}
```

```json
{
  "status": "success",
  "taskID": 1639259414,
  "data": {
    "format": "GEOTIFF",
    "labels": [1],
    "urls": ["https://earthengine.googleapis.com/v1/..."],
    "errors": [null],
    "resolution": 0.25,
    "bbox": { "W": -18, "S": 4, "E": 16, "N": 51 },
    "size": [136, 188],
    "time2GetUrls": 11.61,
    "includeMask": true,
    "maskThreshold": 0.9
  }
}
```

---

## 2. Pressure Timeseries

`POST https://glp.mgravey.com/GeoPressure/v2/timeseries/`

Returns ERA5 surface pressure at one location, either over a time range or at the timestamps of a geolocator timeseries (which also returns its altitude). `dataset="single-levels"` is the default and retains locations over water. With `dataset="land"`, ocean locations are moved to the nearest ERA5-Land pixel within 1000 km.

Altitude defaults to `altitudeFormula="virtual"`, using the same humidity correction and GPT3 lapse rate as [`/pressurePath/`](#altitude-formula) and GeoPressureR, based on the [altitude validation](https://github.com/GeoPressure/altitude-validation). `altitudeFormula="standard"` retains the previous temperature profile. Use `dataset="single-levels"` for altitude because ERA5-Land pressure and orography are inconsistent.

### Request

Provide either `time` + `pressure`, or `startTime` + `endTime`.

| Parameter | Type | Required | Description |
| --------- | ---- | -------- | ----------- |
| `lon`, `lat` | `number` | ✅ | Location (degrees) |
| `time` | `number[]` | ⚠️ | UNIX timestamps of the geolocator measurements |
| `pressure` | `number[]` | ⚠️ | Geolocator pressure (Pa) |
| `startTime`, `endTime` | `number` | ⚠️ | Time range (UNIX timestamps), used when `time`/`pressure` are absent |
| `dataset` | `string` | | `"single-levels"` (default) or `"land"` |
| `altitudeFormula` | `string` | | `"virtual"` (default) or `"standard"` |

### Response

| Field | Type | Description |
| ----- | ---- | ----------- |
| `format` | `string` | `"csv"` |
| `url` | `string` | CSV download URL |
| `lon`, `lat` | `number` | Location actually used |
| `distInter` | `number` | Distance the location was moved (m; `0` if on land) |

The CSV has columns `time`, `pressure` (ERA5 surface pressure) and, with geolocator `pressure`, `altitude`.

If `time` (or `endTime`) is after the last available ERA5 hour of the selected `dataset`, the request fails with HTTP `416` and a `lastERA5` field (ms), as on [`/map/`](#1-pressure-map).

### Example

```http
POST https://glp.mgravey.com/GeoPressure/v2/timeseries/
Content-Type: application/json

{ "lon": 6, "lat": 46, "startTime": 1497916800, "endTime": 1500667800 }
```

```json
{
  "status": "success",
  "taskID": 1639259414,
  "data": {
    "format": "csv",
    "url": "https://earthengine-highvolume.googleapis.com/v1/...",
    "lon": 6.0,
    "lat": 46.0,
    "distInter": 0
  }
}
```

---

## 3. Ground Elevation Along a Path

`POST https://glp.mgravey.com/GeoPressure/v2/elevationPath/`

Samples the ground elevation (Copernicus GLO DEM) at regular intervals along a polyline and returns percentiles of the elevation within each sampled pixel.

### Request

| Parameter | Type | Required | Default | Description |
| --------- | ---- | -------- | ------- | ----------- |
| `lon`, `lat` | `number[]` | ✅ | | Path vertices (degrees). Alternatively `path` as `[[lon, lat], ...]` |
| `scale` | `number` | ✅ | | DEM resolution (pixels per degree) |
| `samplingScale` | `number` | ✅ | | Sampling interval along the path (samples per degree, i.e. every ~111 km / `samplingScale`) |
| `percentile` | `number[]` | | `[10, 50, 90]` | Elevation percentiles; only `10, 20, …, 90` are available, others return empty arrays |

### Response

| Field | Type | Description |
| ----- | ---- | ----------- |
| `percentileData` | `object` | One array per sampled point (see below) |
| `scale` | `number` | DEM resolution (m) |
| `samplingScale` | `number` | Sampling interval (m) |
| `percentile` | `number[]` | Percentiles requested |

`percentileData` contains `"10"`, `"50"`, … (elevation, m), `lon`, `lat` (sample position), `distance` (cumulative distance along the path, m) and `stapId` (fractional index along the input vertices: `0` = first vertex, `1.5` = halfway between the second and third).

### Example

```http
POST https://glp.mgravey.com/GeoPressure/v2/elevationPath/
Content-Type: application/json

{
  "lon": [8.47, 9.41, 9.01, -0.91, 14.24, 27.30, 34.39, 30.00],
  "lat": [48.89, 44.78, 40.07, 37.68, 17.33, 7.32, 8.09, -23.13],
  "scale": 10,
  "samplingScale": 1,
  "percentile": [10, 50, 90]
}
```

```json
{
  "status": "success",
  "taskID": 1639259414,
  "data": {
    "percentileData": {
      "10": [199, 742, 1649, 556, 302, 566, ...],
      "50": [206, 762, 1732, 600, 313, 587, ...],
      "90": [213, 779, 1812, 641, 330, 607, ...],
      "distance": [0, 111221, 222423, 333606, 444770, 462486, ...],
      "lat": [48.917, 47.918, 46.92, 45.922, 44.923, 44.823, ...],
      "lon": [8.444, 8.743, 8.943, 9.143, 9.343, 9.442, ...],
      "stapId": [0, 0.24, 0.48, 0.72, 0.96, 1, ...]
    },
    "scale": 11113.9,
    "samplingScale": 111139.0,
    "percentile": [10, 50, 90]
  }
}
```

---

## 4. Pressure Data Along a Path

`POST https://glp.mgravey.com/GeoPressure/v2/pressurePath/`

Extracts ERA5 variables at each `(lon, lat, time)` of a path and, if geolocator `pressure` is provided, computes its altitude with the barometric formula.

### Altitude formula

`altitude` is computed with the barometric formula, taking the reference level from ERA5 at the location and hour of each point (surface pressure, 2 m temperature and orography). `altitudeFormula` sets the temperature profile of the air column above it:

- `"virtual"` (default): 2 m virtual temperature, which accounts for humidity (computed from `dewpoint_temperature_2m`), and the lapse rate of the [GPT3](https://doi.org/10.1007/s00190-017-1066-2) empirical troposphere model at the point and day (a mean, annual and semi-annual cycle on a 1° grid, interpolated bilinearly). The standard column is too cold, so it underestimates flight altitude. Against 629 radiosonde stations, at 2400 m above the ground `"virtual"` reduces the bias from −30.9 m to −6.4 m and the median absolute error from 29.8 m to 21.2 m, and it leaves the altitude on the ground unchanged. It is the formula of [`pressure_to_altitude()`](https://github.com/GeoPressure/GeoPressureR/blob/main/R/pressure_to_altitude.R) in GeoPressureR ≥ 3.7.0, and gives the same altitude to within 0.1 m.
- `"standard"`: 2 m temperature and the standard lapse rate of −6.5 K/km, the formula used until 2026.

See the [altitude validation report](https://geopressure.github.io/altitude-validation/) for details.

> [!WARNING]
> **Use `dataset="single-levels"` (the default) whenever you need `altitude`.**
> ERA5-Land's `surface_pressure` is not hydrostatically consistent with the orography ERA5-Land publishes — the two disagree by up to ~10 hPa in steep terrain — and that error passes straight into the retrieved altitude. Against 41,653 hourly observations from 271 NOAA ISD stations (2–3576 m, Alps, July 2020), every station-hour where both products have data:
>
> | `dataset` | bias | MAE | RMSE |
> | --------- | ---- | --- | ---- |
> | `"single-levels"` | −0.6 m | **9.0 m** | 27.4 m |
> | `"land"` / `"both"` | +2.5 m | **55.3 m** | 76.9 m |
>
> `"land"` and `"both"` are kept for backward compatibility and remain fine for other variables. ERA5-Land is forced by ERA5, so it holds no independent information about absolute altitude.

### Request

| Parameter | Type | Required | Default | Description |
| --------- | ---- | -------- | ------- | ----------- |
| `lon`, `lat` | `number[]` | ✅ | | Positions (degrees). Alternatively `path` as `[[lon, lat], ...]` |
| `time` | `number[]` | ✅ | | UNIX timestamps, same length as the positions |
| `variable` | `string[]` | ✅ | | Earth Engine band names of [ERA5](https://developers.google.com/earth-engine/datasets/catalog/ECMWF_ERA5_HOURLY#bands) or [ERA5-Land](https://developers.google.com/earth-engine/datasets/catalog/ECMWF_ERA5_LAND_HOURLY#bands) (without the `_hourly` suffix), plus `altitude` |
| `pressure` | `number[]` | | | Geolocator pressure (Pa), same length; enables `altitude` |
| `dataset` | `string` | | `"single-levels"` | `"single-levels"` (ERA5), `"land"` (ERA5-Land) or `"both"` (ERA5 with ERA5-Land bands taking precedence) |
| `altitudeFormula` | `string` | | `"virtual"` | `"virtual"` or `"standard"` (see [Altitude formula](#altitude-formula)) |
| `workers` | `number` | | `10` | Number of chunks processed in parallel |

### Response

`data` contains one array per requested variable, plus `time` (the input timestamps) and `altitude` (if `pressure` was provided). Points where any requested value is missing (e.g. over water with `"land"`) are dropped, so arrays can be shorter than the input. If `time` is after the last available ERA5 hour of the selected `dataset`, the request fails with HTTP `416` and a `lastERA5` field (ms), as on [`/map/`](#1-pressure-map).

When `pressure` is provided with `dataset="land"` or `"both"`, the response has an extra top-level `warning` field.

### Example

```http
POST https://glp.mgravey.com/GeoPressure/v2/pressurePath/
Content-Type: application/json

{
  "lon": [17.5, 17.5, 17.5, 41.6, 41.6],
  "lat": [48.5, 48.5, 48.5, 41.6, 41.6],
  "time": [1501113600, 1501115400, 1501117200, 1501745400, 1501747200],
  "variable": ["surface_pressure", "temperature_2m"],
  "pressure": [98900, 99200, 99400, 100000, 100100],
  "workers": 1
}
```

```json
{
  "status": "success",
  "taskID": 1639259414,
  "data": {
    "altitude": [151.1, 125.5, 107.6, 90.1, 80.1],
    "surface_pressure": [97973, 97973, 97963.9, 96977.1, 96969.7],
    "temperature_2m": [288.19, 288.19, 288.45, 295.59, 296.25],
    "time": [1501113600, 1501115400, 1501117200, 1501745400, 1501747200]
  }
}
```

---

## Data Sources & Accuracy

- **ERA5 single levels**: hourly, ~0.25°, 1940 to ~1 week before present, global.
- **ERA5-Land**: hourly, ~0.1°, 1950 to ~1 week before present, land only.
- **Ground elevation**: Copernicus GLO DEM, pre-aggregated at several resolutions.

### Accuracy of retrieved altitude

Validated against real barometers (293 NOAA ISD stations, 2–3576 m, 44,163 hourly observations) with `dataset="single-levels"`. The error has two parts:

| Component | Size | Notes |
| --------- | ---- | ----- |
| Static per-site offset | median 3.7 m, p90 15 m | station metadata + sub-grid terrain; constant in time |
| Temporal scatter | median SD **3.1 m**, p90 7.7 m | the actual reanalysis error |

Expect **~3 m for relative altitude changes at a fixed location** and **~10 m mean absolute error for absolute altitude**, degrading to tens of metres in steep terrain — as a fixed offset, not noise. Precision is nearly independent of flight altitude: de-biased RMSE stays 2–7 m up to 1000 m above the model surface and ~12 m at 1000–3000 m above it.

---

## Deployment

Each folder (`map/`, `timeseries/`, `elevationPath/`, `pressurePath/`) is a standalone [Cloud Run](https://cloud.google.com/run) service, `geopressureapi-<folder>` (lower case) in project `cloud-service-mgravey`, region `us-central1`. It is written as a Python function (`functions-framework`, entry point named after the folder, e.g. `pressurePath`) with its own `requirements.txt`. `https://glp.mgravey.com/GeoPressure/v2/<endpoint>/` redirects (307) to the service URL.

**Merging to `main` deploys.** Every push to `main` of GeoPressure/GeoPressureAPI starts four Cloud Build triggers, one per folder, whatever files changed. Each one:

1. builds an image from its folder only, with Google Cloud buildpacks (`pack build --path=<folder>`, `GOOGLE_FUNCTION_TARGET=<folder>`);
2. pushes it to `us-central1-docker.pkg.dev/cloud-service-mgravey/cloud-run-source-deploy/geopressureapi/geopressureapi-<folder>:<commit>`;
3. runs `gcloud run services update geopressureapi-<folder> --image=…:<commit>`, which labels the service `commit-sha=<commit>`.

So a function can only import files inside its own folder. If a build fails, the previous revision keeps serving. To check a deploy after a merge:

```bash
gcloud builds list --project cloud-service-mgravey --limit 4
```

All four builds of the merge commit should be `SUCCESS`. Then compare the [`provenance.code`](#provenance) of each endpoint with the merged code (`sourceHash`, and `commit` once the triggers set it):

```bash
python3 provenance.py map timeseries pressurePath elevationPath
```

The virtual formula and the ERA5 cell rule (`era5_grid_snap()`) are defined in the root `altitude.py`, which computes the GPT3 lapse rate of each point in Python from `gpt3_1_lapse_rate.csv.gz` (the lapse-rate columns of [gpt3_1.grd](https://vmf.geo.tuwien.ac.at/codes/gpt3_1.grd), K/km) and passes it to Earth Engine. `pressurePath/` and `timeseries/` each carry an identical copy of both files, because a function cannot import from outside its folder. After editing either file, copy it into both folders; `tests/test_altitude_copies.py` fails while the copies differ. In the same way, each of the four folders carries a copy of the root `provenance.py`, which builds the [`provenance`](#provenance) field; `tests/test_provenance.py` fails while the copies differ. Run the offline tests with `python3 -m unittest tests.test_timeseries_altitude tests.test_altitude_copies tests.test_era5_grid_snap tests.test_provenance` (the `/map/` request tests also need `numpy`). Map altitude masks retain the standard formula until the cost of virtual temperature on full maps has been benchmarked.

Each function authenticates to Earth Engine with a service account read from two environment variables:

- `GEE_API_ADDRESS`: service account email
- `GEE_API_KEY`: service account private key

On Cloud Run both are [Secret Manager](https://cloud.google.com/secret-manager) references on each service, and `gcloud run services update` keeps them from one deploy to the next.

Two optional variables fill `provenance.code` (see [Provenance](#provenance)):

- `GIT_COMMIT`: full SHA of the deployed commit. The service label `commit-sha` is not visible inside the container, so the Deploy step of each trigger has to set it, with `--update-env-vars=GIT_COMMIT=$COMMIT_SHA`.
- `DEPLOYED_AT`: deploy time, ISO 8601 UTC. Cloud Build has no substitution for the time, so it is unset by default (`null`). The deploy time is the creation time of the revision named in `provenance.code.revision`.

Cloud Run sets `K_SERVICE` and `K_REVISION`.

To run one locally:

```bash
cd pressurePath && pip install -r requirements.txt && functions-framework --target=pressurePath
```

`map` uses the standard Earth Engine endpoint; the other services use the high-volume endpoint. The [Test Server](.github/workflows/test-server.yml) workflow calls the live endpoints every 12 hours.
