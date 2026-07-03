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
        self, path, time, pressure, variable, nbChunk=10, dataset="single-levels"
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
            dataset (str): Data source - "land", "single-levels", or "both"

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
            chunkSize = (size // nbChunk) + 1
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
                - Lb: Standard temperature lapse rate (-6.5 K/km)
                - R: Universal gas constant (8.31432 J/mol/K)
                - g0: Standard gravity (9.80665 m/s²)
                - M: Molar mass of dry air (0.0289644 kg/mol)

                Args:
                    ft: Earth Engine Feature with matched ERA5 data

                Returns:
                    Earth Engine Feature with sampled variables
                """
                # Physical constants for barometric formula
                Lb = -0.0065  # Standard temperature lapse rate [K/m]
                R = 8.31432  # Universal gas constant [J/mol/K]
                g0 = 9.80665  # Gravitational acceleration [m/s²]
                M = 0.0289644  # Molar mass of Earth's air [kg/mol]

                # Calculate altitude using barometric formula
                # h = (T/Lb) * ((P/P0)^(-R*Lb/g0/M) - 1) + h0
                im=self.ee.Image(ft.get("bestERA5"));
                dh = (
                    im
                    .select("temperature_2m")
                    .divide(Lb)
                    .multiply(
                        self.ee.Image.constant(self.ee.Number(ft.get("pressure")))
                        .divide(
                            self.ee.Image(ft.get("bestERA5")).select("surface_pressure")
                        )
                        .pow(-R * Lb / g0 / M)
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

        # Initialize processing
        size = len(path)
        results = [None] * nbChunk

        # Process chunks in parallel
        with ThreadPoolExecutor(max_workers=min(nbChunk,90)) as executor:
            executor.map(runComputation4Chunk, list(range(nbChunk)), [val] * nbChunk)

        # Filter out empty results and combine chunks
        results = [x for x in results if x is not None]

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
            - dataset: "land", "single-levels", or "both" (default)
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

        # Process optional dataset parameter
        dataset = "single-levels"
        if "dataset" in jsonObj.keys():
            if isinstance(jsonObj["dataset"], list):
                dataset = jsonObj["dataset"][0]
            else:
                dataset = jsonObj["dataset"]

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
                path, time, pressure, variable, workers, dataset
            )
            response = {"status": "success", "taskID": timeStamp, "data": data}
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
