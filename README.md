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
- **Success:** `{"status": "success", "taskID": ..., "data": {...}}`. The fields documented below are inside `data`.
- **Error:** HTTP `400` with `{"status": "error", "taskID": ..., "errorMessage": ..., "advice": ...}`.

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

If `time` extends past the last available ERA5 hour of the selected `dataset`, the request fails with HTTP `416` and a `lastERA5` field (ms).

Each GeoTIFF contains:

- **Band 1 – MSE**: [mean square error](https://en.wikipedia.org/wiki/Mean_squared_error) between geolocator and ERA5 surface pressure, after removing the mean of each (so an unknown constant altitude offset does not matter).
- **Band 2 – Mask** _(if `includeMask`)_: proportion of measurements whose altitude, from the [barometric formula](https://en.wikipedia.org/wiki/Barometric_formula), falls within the ground elevation range of the pixel (Copernicus GLO DEM min/max) ± `margin`.
- **Special values:** `-1` below `maskThreshold`, `-2` water / no data.

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

Altitude defaults to `altitudeFormula="virtual"`, using the same humidity correction and seasonal, latitude-dependent lapse rate as [`/pressurePath/`](#altitude-formula) and GeoPressureR, based on the [altitude validation](https://github.com/GeoPressure/altitude-validation). `altitudeFormula="standard"` retains the previous temperature profile. Use `dataset="single-levels"` for altitude because ERA5-Land pressure and orography are inconsistent.

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

- `"virtual"` (default): 2 m virtual temperature, which accounts for humidity (computed from `dewpoint_temperature_2m`), and a lapse rate varying with season and latitude, from −6.6 K/km in the tropics to about −3 K/km in a mid-latitude winter (capped at −2 K/km). The standard column is too cold, so it underestimates flight altitude by 1–1.5% of the height above the ground. Against 648 radiosonde stations, over the heights birds fly, `"virtual"` reduces the bias from −11.9 m to −2.3 m and the mean absolute error from 16.4 m to 13.0 m, and leaves the altitude on the ground unchanged. It is the formula of [`pressure_to_altitude()`](https://github.com/GeoPressure/GeoPressureR/blob/main/R/pressure_to_altitude.R) in GeoPressureR.
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

`data` contains one array per requested variable, plus `time` (the input timestamps) and `altitude` (if `pressure` was provided). Points where any requested value is missing (e.g. over water with `"land"`) are dropped, so arrays can be shorter than the input.

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

Each folder (`map/`, `timeseries/`, `elevationPath/`, `pressurePath/`) is a standalone [Google Cloud Function](https://cloud.google.com/functions) (Python, `functions-framework`) with its own `requirements.txt`. The entry point has the same name as the folder (e.g. `pressurePath`).

The virtual formula and its fitted coefficients are defined once in `altitude.py`, shared by `pressurePath` and `timeseries`. Map altitude masks retain the standard formula until the cost of virtual temperature on full maps has been benchmarked.

Stage a function before deployment so its source includes the shared module:

```bash
python scripts/prepare_function.py pressurePath /tmp/pressurePath-source
# Use /tmp/pressurePath-source as the deployment source directory.
```

Each function authenticates to Earth Engine with a service account read from two environment variables:

- `GEE_API_ADDRESS`: service account email
- `GEE_API_KEY`: service account private key

To run one locally:

```bash
cd pressurePath && pip install -r requirements.txt && PYTHONPATH=.. functions-framework --target=pressurePath
```

`map` uses the standard Earth Engine endpoint; the other services use the high-volume endpoint. The [Test Server](.github/workflows/test-server.yml) workflow calls the live endpoints every 12 hours.
