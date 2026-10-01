#!/usr/local/bin/python3
"""
GeoPressure API - Pressure Path Module

This module provides functionality to extract atmospheric pressure and meteorological
variables from ERA5/ERA5-LAND reanalysis data along specified paths with optional
geolocator pressure data for altitude computation.

Author: GeoPressure Team
"""

import datetime
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor

from GEE_API_server import GEE_Service

# Get number of CPU cores for parallel processing
numCores = os.cpu_count()

# Datasets whose altitude output cannot be trusted. ERA5-LAND's surface_pressure is not
# the exact hydrostatic image of the orography ERA5-LAND publishes: the two disagree by
# up to ~10 hPa in steep terrain. The orography term therefore does not cancel out of the
# barometric formula, and the retrieved altitude inherits the whole discrepancy.
# Validated against 41653 hourly station-pressure observations from 271 NOAA ISD stations
# (2-3576 m, Alps, July 2020): mean absolute error 9.0 m for "single-levels" against
# 55.3 m for "land". ERA5-LAND has no atmospheric analysis of its own -- it is forced by
# ERA5 -- so it carries no independent information about absolute altitude.
DATASET_ALTITUDE_UNSAFE = ("land", "both")

DATASET_ALTITUDE_WARNING = (
    'dataset="{}" is not suitable for altitude: ERA5-LAND surface_pressure is not '
    "hydrostatically consistent with ERA5-LAND orography, giving an altitude error of "
    "tens to hundreds of metres in steep terrain (mean absolute error 55 m against "
    'station barometers, versus 9 m for dataset="single-levels"). The returned '
    '"altitude" array should not be used. Use dataset="single-levels" instead.'
)

# Formulas available to convert pressure to altitude (request parameter
# "altitudeFormula"):
#   "standard": 2 m temperature and the ICAO lapse rate of -6.5 K/km, as GeoPressureR's
#       pressure_to_altitude(). Default, so that source = "api" and source = "arco" in
#       GeoPressureR keep giving the same altitude.
#   "virtual": 2 m virtual temperature (from the 2 m dewpoint) and a lapse rate varying
#       with season and latitude. The standard column is too cold, which underestimates
#       flight altitude by 1-1.5% of the height above the ground. Against 648 IGRA2
#       radiosonde stations, weighted by the heights birds fly, this formula lowers the
#       bias from -11.9 m to -2.3 m and the mean absolute error from 16.4 m to 13.0 m on
#       held-out stations, without changing the altitude on the ground. See
#       https://geopressure.github.io/altitude-validation/
ALTITUDE_FORMULAS = ("standard", "virtual")

# Lapse rate of the "virtual" formula, in K/km:
#   L = b1 + b2 a + (b3 + b4 a) cos(2 pi (d - 15) / 365.25)
# with a = |latitude| / 90 and d the day of year, shifted by six months in the southern
# hemisphere: a mean lapse rate and a cycle peaking in mid-winter, both changing linearly
# with latitude. Fitted on the IGRA2 radiosondes; L is kept between -9.5 and -2 K/km.
LAPSE_COEF = (-6.6306, 3.7168, -0.0744, 3.1974)
LAPSE_RANGE = (-9.5, -2.0)


def printErrorMessage(task_id, errorMessage, adviceMessage="Double check the inputs."):
    """
    Create standardized error response for API endpoints.

    Args:
        task_id (int): Unique task identifier
        errorMessage (str): Description of the error
        adviceMessage (str): Guidance for resolving the error

    Returns:
        tuple: (status_code, headers, json_response)
    """
    return (
        400,
        {"Content-type": "application/json"},
        json.JSONEncoder().encode(
            {
                "status": "error",
                "taskID": task_id,
                "errorMessage": errorMessage,  # Fixed typo: was "errorMesage"
                "advice": adviceMessage,
            }
        ),
    )


class GP_pressurePath(GEE_Service):
    """
    GeoPressure Path Analysis Class

    Extracts atmospheric variables from ERA5/ERA5-LAND data along paths with
    optional altitude computation from geolocator pressure measurements.

    Supports:
    - ERA5-LAND hourly data
    - ERA5 single-levels hourly data
    - Combined datasets with intelligent band merging
    - Parallel processing for large datasets
    - Altitude computation using barometric formula
    """

    def __init__(self, service_account, apiKeyFile, highvolume=False):
        """
        Initialize the GP_pressurePath service.

        Args:
            service_account (str): Google Earth Engine service account email
            apiKeyFile (str): Path to service account JSON key file
            highvolume (bool): Use high-volume Earth Engine endpoint
        """
        super(GP_pressurePath, self).__init__(service_account, apiKeyFile, highvolume)

        geoPotLand = self.ee.Image(
            "projects/earthimages4unil/assets/PostDocProjects/rafnuss/Geopot_ERA5"
        ).multiply(9.80665).rename("geopotential")
        geoPotLevel = self.ee.Image("ECMWF/ERA5/HOURLY/20200101T00").select("geopotential")

        def addGeopotLand(im):
            return im.addBands(geoPotLand.updateMask(im.select("temperature_2m").mask()))

        def addGeopotLevel(im):
            return im.addBands(geoPotLevel)

        era5_land = self.ee.ImageCollection("ECMWF/ERA5_LAND/HOURLY").map(addGeopotLand)
        era5_single = self.ee.ImageCollection("ECMWF/ERA5/HOURLY").map(addGeopotLevel)
        
        strToRemove=self.ee.String("_hourly");

        def removeHourly(str):
            return self.ee.String(str).slice(0,strToRemove.length().multiply(-1))

        oldNName=era5_land.first().bandNames().filter(self.ee.Filter.stringEndsWith('item',strToRemove));
        newName=oldNName.map(removeHourly)

        def addRenamed(im):
            return im.addBands(im.select(oldNName,newName),None,True);
		
        era5_land=era5_land.map(addRenamed);

        # Create index-based filter for joining collections
        indexFilter = self.ee.Filter.equals(
            leftField="system:index", rightField="system:index"
        )

        # Join ERA5 and ERA5-LAND collections by timestamp
        simpleJoin = self.ee.Join.saveFirst("match")
        simpleJoined = simpleJoin.apply(era5_single, era5_land, indexFilter)

        def combineBands(image):
            """
            Combine bands from ERA5 and ERA5-LAND collections.

            For overlapping bands, prioritizes ERA5-LAND data over ERA5 single-levels.
            This provides the best available land surface data while maintaining
            atmospheric column variables from ERA5.

            Args:
                image: ERA5 image with matched ERA5-LAND image in 'match' property

            Returns:
                Combined image with all available bands
            """
            land_image = self.ee.Image(image.get("match"))
            commonBands = land_image.bandNames().filter(
                self.ee.Filter.inList("item", image.bandNames())
            )
            return image.addBands(
                self.ee.ImageCollection([image, land_image])
                .select(commonBands)
                .mosaic(),
                None,
                True,
            )

        # Create combined ERA5 collection with merged bands
        self.ERA5Combined = self.ee.ImageCollection(simpleJoined).map(combineBands)
        self.era5_land=era5_land;
        self.era5_single=era5_single;

    def getPressureAlongPath(  # Fixed typo: was "getPresureAlongPath"
        self,
        path,
        time,
        pressure,
        variable,
        nbChunk=10,
        dataset="single-levels",
        altitudeFormula="standard",
    ):
        """
        Extract atmospheric variables along a path with optional altitude computation.

        This method processes coordinate-time-pressure triplets to extract ERA5
        atmospheric variables. When pressure data is provided, it computes altitude
        using the barometric formula accounting for temperature and geopotential.

        Args:
            path (list): List of [lon, lat] coordinate pairs
            time (list): List of UNIX timestamps (seconds since 1970-01-01)
            pressure (list, optional): Geolocator pressure values (Pascal)
            variable (list): ERA5 variable names to extract
            nbChunk (int): Number of parallel processing chunks (default: 10)
            dataset (str): Data source - "single-levels" (default), "land", or "both".
                Use "single-levels" whenever altitude matters: ERA5-LAND's
                surface_pressure is not hydrostatically consistent with ERA5-LAND's own
                orography, so "land" and "both" carry an altitude error of up to a few
                hundred metres in steep terrain. See DATASET_ALTITUDE_WARNING.
            altitudeFormula (str): "standard" (default) or "virtual". See
                ALTITUDE_FORMULAS.

        Returns:
            dict: Arrays for time, variables, and altitude (if pressure provided)

        Raises:
            Exception: If data processing fails or invalid parameters provided
        """

        def makeFeature(measurement_list):
            """
            Convert list of [time, pressure, coordinates] to Earth Engine Feature.

            Args:
                measurement_list: Earth Engine List containing [time, pressure, [lon, lat]]

            Returns:
                Earth Engine Feature with point geometry and properties
            """
            measurement_list = self.ee.List(measurement_list)
            return self.ee.Feature(
                self.ee.Geometry.Point(measurement_list.get(2)),
                {
                    "system:time_start": self.ee.Number(
                        measurement_list.get(0)
                    ).multiply(
                        1000
                    ),  # Convert to milliseconds
                    "pressure": measurement_list.get(1),
                },
            )

        # Prepare data arrays - use dummy pressure if not provided
        val = self.ee.List(
            [time, pressure, path] if pressure else [time, [0] * len(time), path]
        ).unzip()

        def runComputation4Chunk(i, val):
            """
            Process a chunk of data points in parallel.

            This function handles the core computation for a subset of the input data,
            including ERA5 data matching, altitude calculation, and variable extraction.

            Args:
                i (int): Chunk index
                val: Earth Engine List of input data
            """

            # Calculate chunk boundaries
            localVal = val.slice(i * chunkSize, min((i + 1) * chunkSize, size))
            fc = self.ee.FeatureCollection(localVal.map(makeFeature))

            # Get time range for this chunk
            start = fc.aggregate_min("system:time_start")
            end = fc.aggregate_max("system:time_start")

            # Select appropriate ERA5 dataset
            if dataset.lower() == "single-levels":
                ERA5 = self.era5_single
            elif dataset.lower() == "land":
                ERA5 = self.era5_land
            else:
                ERA5 = self.ERA5Combined

            # Filter ERA5 data by time range
            ERA5_pressure = ERA5.filterDate(  # Fixed typo: was "ERA5_pressur"
                start, self.ee.Date(end).advance(1, "hour")
            )

            # Match each point with closest ERA5 timestamp (within 1 hour)
            era5_labelFeature = (
                self.ee.Join.saveBest(  # Fixed typo: was "era5_llabelFeature"
                    matchKey="bestERA5", measureKey="diff"
                ).apply(
                    fc,
                    ERA5_pressure,
                    self.ee.Filter.maxDifference(
                        3600 * 1000,  # 1 hour in milliseconds
                        leftField="system:time_start",
                        rightField="system:time_start",
                    ),
                )
            )

            def getAltitude(ft):
                """
                Calculate altitude using barometric formula and extract variables.

                Uses the hypsometric equation to compute altitude from pressure difference,
                accounting for temperature variation and geopotential height from ERA5.

                Physical constants from standard atmosphere:
                - Lb: Temperature lapse rate (-6.5 K/km, or varying with season and
                  latitude for altitudeFormula="virtual")
                - R: Universal gas constant (8.31432 J/mol/K)
                - g0: Standard gravity (9.80665 m/s²)
                - M: Molar mass of dry air (0.0289644 kg/mol)

                Args:
                    ft: Earth Engine Feature with matched ERA5 data

                Returns:
                    Earth Engine Feature with sampled variables
                """
                # Physical constants for barometric formula
                R = 8.31432  # Universal gas constant [J/mol/K]
                g0 = 9.80665  # Gravitational acceleration [m/s²]
                M = 0.0289644  # Molar mass of Earth's air [kg/mol]

                im = self.ee.Image(ft.get("bestERA5"))
                if altitudeFormula == "virtual":
                    # 2 m virtual temperature Tv = T (1 + 0.608 q), with the specific
                    # humidity q from the 2 m dewpoint and the vapour pressure from
                    # Bolton (1980)
                    td = im.select("dewpoint_temperature_2m").subtract(273.15)
                    e = td.multiply(17.67).divide(td.add(243.5)).exp().multiply(611.2)
                    T = im.expression(
                        "T * (1 + 0.608 * 0.622 * e / (sp - 0.378 * e))",
                        {
                            "T": im.select("temperature_2m"),
                            "sp": im.select("surface_pressure"),
                            "e": e,
                        },
                    )
                    # Lapse rate varying with season and latitude (see LAPSE_COEF)
                    lat = ft.geometry().coordinates().getNumber(1)
                    doy = (
                        self.ee.Date(ft.get("system:time_start"))
                        .getRelative("day", "year")
                        .add(1)
                    )
                    sdoy = self.ee.Number(
                        self.ee.Algorithms.If(
                            lat.lt(0), doy.add(182).mod(365).add(1), doy
                        )
                    )
                    a = lat.abs().divide(90)
                    c = sdoy.subtract(15).multiply(2 * math.pi / 365.25).cos()
                    b1, b2, b3, b4 = LAPSE_COEF
                    Lb = (
                        a.multiply(b2)
                        .add(b1)
                        .add(a.multiply(b4).add(b3).multiply(c))
                        .clamp(*LAPSE_RANGE)
                        .divide(1000)
                    )
                else:
                    T = im.select("temperature_2m")
                    Lb = self.ee.Number(-0.0065)  # Standard lapse rate [K/m]

                # Calculate altitude using barometric formula
                # h = (T/Lb) * ((P/P0)^(-R*Lb/g0/M) - 1) + h0
                dh = (
                    T.divide(Lb)
                    .multiply(
                        self.ee.Image.constant(self.ee.Number(ft.get("pressure")))
                        .divide(im.select("surface_pressure"))
                        .pow(Lb.multiply(-R / g0 / M))
                        .subtract(1)
                    )
                    .add(im.select("geopotential").divide(g0))
                    .rename("altitude")
                )

                # Combine altitude with ERA5 variables and timestamp
                return (
                    dh.addBands(self.ee.Image(ft.get("bestERA5")))
                    .addBands(
                        self.ee.Image.constant(
                            self.ee.Number(ft.get("system:time_start")).divide(1000)
                        )
                        .rename("time")
                        .toLong()
                    )
                    .sample(region=ft.geometry(), scale=1, numPixels=1, dropNulls=False)
                    .first()
                    .set(
                        "ERA5_ID", self.ee.Image(ft.get("bestERA5")).get("system:index")
                    )
                )

            # Process all features in this chunk
            aggregatedMap = self.ee.FeatureCollection(  # Fixed typo: was "agregatedMap"
                era5_labelFeature.map(getAltitude)
            )

            # Filter out null values for required variables
            required_vars = list(
                set(["time"] + (["altitude"] if pressure else []) + variable)
            )
            for key in required_vars:
                aggregatedMap = aggregatedMap.filter(self.ee.Filter.neq(key, None))

            def toJson(key, val):
                """Extract array values for a given variable."""
                return aggregatedMap.aggregate_array(val)

            # Create dictionary of variable arrays
            js = self.ee.Dictionary.fromLists(required_vars, required_vars).map(toJson)

            # Store results for this chunk
            results[i] = js.getInfo()

        # Initialize processing. Chunk sizes must be rounded UP and the chunk count
        # derived back from them, otherwise the last chunks slice an empty range -- e.g.
        # 2 points over 2 workers used to give a chunk size of 2, leaving chunk 2 empty,
        # and aggregate_min on an empty collection yields a null date that Join rejects.
        size = len(path)
        nbChunk = max(1, min(nbChunk, size))
        chunkSize = -(-size // nbChunk)  # ceil
        nbChunk = -(-size // chunkSize)  # ceil; every chunk now holds >= 1 point
        results = [None] * nbChunk

        # Process chunks in parallel. The futures must be collected and their results
        # read: ThreadPoolExecutor.map returns a lazy generator, so simply calling it
        # discards any exception a chunk raised. That used to turn an Earth Engine
        # failure in one chunk into silently missing points in a "success" response.
        with ThreadPoolExecutor(max_workers=min(nbChunk, 90)) as executor:
            futures = [
                executor.submit(runComputation4Chunk, i, val) for i in range(nbChunk)
            ]
            for i, future in enumerate(futures):
                try:
                    future.result()
                except Exception as e:
                    raise Exception(
                        "Failed to process chunk {} of {}: {}".format(i + 1, nbChunk, e)
                    ) from e

        if any(x is None for x in results):
            raise Exception("Chunk processing returned no data; please retry.")

        # Merge all chunk results into single arrays
        return {key: [item for d in results for item in d[key]] for key in results[0]}

    # url=agregatedMap.getDownloadURL(selectors=['time']+variable)
    # return url;

    def singleRequest(self, jsonObj, requestType):
        """
        Handle a single API request for pressure path analysis.

        Validates input parameters, processes the request, and returns results
        in standardized JSON format. Supports both path arrays and separate
        lat/lon arrays for coordinate specification.

        Args:
            jsonObj (dict): JSON request object with required parameters
            requestType: Request type (unused, maintained for compatibility)

        Returns:
            tuple: (status_code, headers, json_response)

        Required Parameters:
            - path OR (lat AND lon): Coordinate specification
            - time: Array of UNIX timestamps
            - variable: Array of ERA5 variable names

        Optional Parameters:
            - pressure: Geolocator pressure measurements
            - dataset: "single-levels" (default), "land", or "both". "land" and "both"
              are kept for backward compatibility only and must not be used when
              altitude is requested; the response then carries a "warning" field.
            - altitudeFormula: "standard" (default) or "virtual" (see ALTITUDE_FORMULAS)
            - workers: Number of processing chunks (default: 10)
        """
        timeStamp = math.floor(datetime.datetime.utcnow().timestamp())

        # Validate JSON object
        if len(jsonObj.keys()) < 1:
            return printErrorMessage(
                timeStamp, "JSON object is empty! Did you send it as JSON by accident?"
            )

        # Validate coordinate parameters
        if "path" not in jsonObj.keys() and not (
            "lat" in jsonObj.keys() and "lon" in jsonObj.keys()
        ):
            return printErrorMessage(
                timeStamp,
                "Path or lat + lon is missing. Should be an array of [lon,lat] or arrays of lat and lon coordinates.",
            )

        # Validate required time parameter
        if "time" not in jsonObj.keys():
            return printErrorMessage(
                timeStamp, "Time parameter is required and should be an array."
            )

        # Validate required variable parameter
        if "variable" not in jsonObj.keys():
            return printErrorMessage(
                timeStamp,
                "Variable parameter is required and should be an array of ERA5 band names.",
            )

        # Process coordinate input - convert lat/lon arrays to path if needed
        if "lat" in jsonObj.keys() and "lon" in jsonObj.keys():
            path = list(zip(jsonObj["lon"], jsonObj["lat"]))
        elif "path" in jsonObj.keys():
            path = jsonObj["path"]

        # Process optional dataset parameter. "single-levels" is the default because it
        # is the only one that yields a trustworthy altitude (see
        # DATASET_ALTITUDE_WARNING); "land" and "both" remain available for callers that
        # explicitly ask for them.
        dataset = "single-levels"
        if "dataset" in jsonObj.keys():
            if isinstance(jsonObj["dataset"], list):
                dataset = jsonObj["dataset"][0]
            else:
                dataset = jsonObj["dataset"]

        # Process optional altitudeFormula parameter
        altitudeFormula = jsonObj.get("altitudeFormula", "standard")
        if isinstance(altitudeFormula, list):
            altitudeFormula = altitudeFormula[0]
        altitudeFormula = str(altitudeFormula).lower()
        if altitudeFormula not in ALTITUDE_FORMULAS:
            return printErrorMessage(
                timeStamp,
                'altitudeFormula must be "standard" or "virtual".',
            )

        # Process optional workers parameter
        workers = 10
        if "workers" in jsonObj.keys():
            if isinstance(jsonObj["workers"], list):
                workers = int(jsonObj["workers"][0])
            else:
                workers = int(jsonObj["workers"])

        # Extract required parameters
        time = jsonObj["time"]
        pressure = jsonObj.get("pressure")  # Optional parameter
        variable = jsonObj["variable"]

        # Validate array lengths
        if (pressure and len(path) != len(pressure)) or len(path) != len(time):
            return printErrorMessage(
                timeStamp, "Pressure, time and path arrays must have the same length."
            )

        try:
            # Process the request
            data = self.getPressureAlongPath(  # Fixed method name
                path, time, pressure, variable, workers, dataset, altitudeFormula
            )
            response = {"status": "success", "taskID": timeStamp, "data": data}
            # Altitude from ERA5-LAND is unreliable; say so rather than returning a
            # plausible-looking number that can be hundreds of metres out.
            if pressure and dataset.lower() in DATASET_ALTITUDE_UNSAFE:
                response["warning"] = DATASET_ALTITUDE_WARNING.format(dataset)
            return (
                200,
                {"Content-type": "application/json"},
                json.JSONEncoder().encode(response),
            )
        except Exception as e:
            import traceback

            traceback.print_exc()
            return printErrorMessage(
                timeStamp,
                str(e),
                "An error occurred during processing. Please try again, and if the problem persists, file an issue at https://github.com/Rafnuss/GeoPressureAPI/issues/new?body=task_id:{}&labels=crash".format(
                    timeStamp
                ),
            )
