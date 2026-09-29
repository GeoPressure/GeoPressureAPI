#!/usr/local/bin/python3
"""
GeoPressure API - Timeseries Module

This module provides functionality to extract time series data from ERA5-LAND atmospheric
reanalysis for specific coordinates. It supports both time-bounded extraction and explicit
pressure-based altitude computation using the barometric formula.

Author: GeoPressure Team
"""

import datetime
import json
import math
import os

from GEE_API_server import GEE_Service


def printErrorMessage(task_id, errorMessage, adviceMessage="Double check the inputs"):
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


class GP_timeseries_v2(GEE_Service):
    """
    GeoPressure Timeseries Analysis Class

    Extracts atmospheric pressure time series from ERA5-LAND data at specific coordinates.
    Supports both simple time-bounded extraction and pressure-based altitude computation
    for geolocator analysis.

    Features:
    - ERA5-LAND hourly atmospheric data extraction
    - Time-bounded pressure series generation
    - Pressure-based altitude computation using barometric formula
    - Automatic land/ocean detection with nearest land interpolation
    - CSV export functionality
    """

    def boundingTimeCollection(self, timeStart, timeEnd, coordinates):
        """
        Extract pressure time series within a specified time range.

        This method extracts all available ERA5-LAND pressure data between
        start and end timestamps for a given coordinate location.

        Args:
            timeStart (int): Start timestamp in milliseconds since epoch
            timeEnd (int): End timestamp in milliseconds since epoch
            coordinates (list): [longitude, latitude] pair

        Returns:
            str: Download URL for CSV containing time and pressure columns
        """

        def reduce2aPixel(image):
            """
            Extract pixel value and add timestamp for each ERA5 image.

            Args:
                image: ERA5-LAND image with surface pressure data

            Returns:
                Earth Engine FeatureCollection with time and pressure values
            """
            return image.addBands(
                self.ee.Image.constant(
                    self.ee.Number(image.get("system:time_start")).divide(1000)
                )
                .rename("time")
                .toLong()
            ).sample(region=self.ee.Geometry.Point(coordinates), scale=10, numPixels=1)

        # Load ERA5-LAND pressure data for specified time range
        ERA5_pressure = (  # Fixed typo: was "ERA5_pressur"
            self.ee.ImageCollection("ECMWF/ERA5_LAND/HOURLY")
            .filterDate(timeStart, self.ee.Date(timeEnd).advance(1, "hour"))
            .select(["surface_pressure"], ["pressure"])
        )

        # Extract pixel values for all timestamps
        feature_collection = ERA5_pressure.map(reduce2aPixel).flatten()

        # Generate download URL for CSV export
        url = self.ee.FeatureCollection(feature_collection).getDownloadURL(
            selectors=["time", "pressure"]
        )
        return url

    def explicitTimeCollection(
        self, time, pressure, coordinates
    ):  # Fixed typo: was "expliciteTimeCollection"
        """
        Compute altitude time series from explicit pressure measurements.

        This method uses observed pressure measurements to compute altitude
        using the barometric formula, matching with closest ERA5 timestamps
        for temperature correction.

        Args:
            time (list): Array of UNIX timestamps (seconds since epoch)
            pressure (list): Array of pressure measurements in Pascal
            coordinates (list): [longitude, latitude] pair

        Returns:
            str: Download URL for CSV containing time, pressure, and altitude
        """

        def makeFeature(measurement_list):
            """
            Convert time-pressure pair to Earth Engine Feature.

            Args:
                measurement_list: List containing [time, pressure] values

            Returns:
                Earth Engine Feature with timestamp and pressure properties
            """
            measurement_list = self.ee.List(measurement_list)
            return self.ee.Feature(
                None,
                {
                    "system:time_start": self.ee.Number(
                        measurement_list.get(0)
                    ).multiply(1000),
                    "pressure": measurement_list.get(1),
                },
            )

        # Combine time and pressure arrays
        val = self.ee.List([time, pressure]).unzip()
        feature_collection = self.ee.FeatureCollection(val.map(makeFeature))

        # Get time range for ERA5 data filtering
        start = feature_collection.aggregate_min("system:time_start")
        end = feature_collection.aggregate_max("system:time_start")

        # Load ERA5-LAND data for temperature and pressure
        ERA5 = self.ee.ImageCollection("ECMWF/ERA5_LAND/HOURLY")
        # Open the window an hour before the first measurement so that it can be
        # matched to the closest image, not only to the next one.
        ERA5_pressure = ERA5.filterDate(  # Fixed typo: was "ERA5_pressur"
            self.ee.Date(start).advance(-1, "hour"), self.ee.Date(end).advance(1, "hour")
        ).select(["surface_pressure", "temperature_2m"])

        # Match each measurement with closest ERA5 timestamp (within 1 hour)
        era5_labelFeature = (
            self.ee.Join.saveBest(  # Fixed typo: was "era5_llabelFeature"
                matchKey="bestERA5", measureKey="diff"
            ).apply(
                feature_collection,
                ERA5_pressure,
                self.ee.Filter.maxDifference(
                    3600 * 1000,  # 1 hour tolerance in milliseconds
                    leftField="system:time_start",
                    rightField="system:time_start",
                ),
            )
        )

        def getAltitude(feature):
            """
            Calculate altitude using barometric formula.

            Uses the hypsometric equation to compute altitude from pressure difference,
            accounting for temperature variation and geopotential height from ERA5.

            Physical constants from standard atmosphere:
            - Lb: Standard temperature lapse rate (-6.5 K/km)
            - R: Universal gas constant (8.31432 J/mol/K)
            - g0: Standard gravity (9.80665 m/s²)
            - M: Molar mass of dry air (0.0289644 kg/mol)

            Args:
                feature: Earth Engine Feature with matched ERA5 data

            Returns:
                Earth Engine FeatureCollection with altitude, pressure, and time
            """
            # Physical constants for barometric formula
            Lb = -0.0065  # Standard temperature lapse rate [K/m]
            R = 8.31432  # Universal gas constant [J/mol/K]
            g0 = 9.80665  # Gravitational acceleration [m/s²]
            M = 0.0289644  # Molar mass of Earth's air [kg/mol]
            T0 = 273.15 + 15  # Standard sea level temperature [K]

            # Load reference geopotential height
            altIm = self.ee.Image(
                "projects/earthimages4unil/assets/PostDocProjects/rafnuss/Geopot_ERA5"
            )

            # Calculate altitude using barometric formula
            # h = (T/Lb) * ((P/P0)^(-R*Lb/g0/M) - 1) + h0
            dh = (
                self.ee.Image(feature.get("bestERA5"))
                .select("temperature_2m")
                .divide(Lb)
                .multiply(
                    self.ee.Image.constant(self.ee.Number(feature.get("pressure")))
                    .divide(
                        self.ee.Image(feature.get("bestERA5")).select(
                            "surface_pressure"
                        )
                    )
                    .pow(-R * Lb / g0 / M)
                    .subtract(1)
                )
                .add(altIm)
                .rename("altitude")
            )

            # Combine altitude with pressure and timestamp data
            return (
                dh.addBands(
                    self.ee.Image(feature.get("bestERA5"))
                    .select("surface_pressure")
                    .rename("pressure")
                )
                .addBands(
                    self.ee.Image.constant(
                        self.ee.Number(feature.get("system:time_start"))
                    )
                    .rename("time")
                    .divide(1000)
                    .toLong()
                )
                .sample(
                    region=self.ee.Geometry.Point(coordinates), scale=1, numPixels=1
                )
            )

        # Process all measurements and generate output
        aggregatedMap = self.ee.FeatureCollection(  # Fixed typo: was "agregatedMap"
            era5_labelFeature.map(getAltitude)
        ).flatten()

        # Generate download URL for CSV export
        url = aggregatedMap.getDownloadURL(selectors=["time", "pressure", "altitude"])
        return url

    def checkPosition(self, coordinates):
        """
        Validate coordinate position and find nearest land point if over ocean.

        This method checks if the specified coordinates are over land using ERA5-LAND
        data mask. If over ocean, it finds the nearest land point within a search radius.

        Args:
            coordinates (list): [longitude, latitude] pair to validate

        Returns:
            tuple: (longitude, latitude, distance, was_changed)
                - longitude: Final longitude (possibly adjusted)
                - latitude: Final latitude (possibly adjusted)
                - distance: Distance to nearest land (0 if originally on land)
                - was_changed: Boolean indicating if coordinates were modified
        """
        lon, lat = coordinates

        # Load ERA5-LAND data and extract land mask
        ERA5 = self.ee.ImageCollection("ECMWF/ERA5_LAND/HOURLY")
        land = ERA5.first().select("surface_pressure").mask()

        # Search parameters
        radius = 1000000  # 1000km search radius in meters
        geometry = self.ee.Geometry.Point(coordinates)

        # Check if current position is over land
        isLand = (
            land.sample(region=geometry, scale=1, numPixels=1)
            .first()
            .get("surface_pressure")
            .getInfo()
        )

        if not isLand:
            # Position is over ocean - find nearest land point
            # Expand land mask by 1 pixel to include coastal areas
            land = land.focalMin(1, "circle", "pixels")

            # Create distance image with longitude/latitude bands
            aoi = (
                self.ee.FeatureCollection(self.ee.Feature(geometry))
                .distance(radius)
                .addBands(self.ee.Image.pixelLonLat())
                .updateMask(land)
            )

            # Find minimum distance location
            best = aoi.reduceRegion(
                self.ee.Reducer.min(3),
                geometry.buffer(radius),
                land.geometry().projection().nominalScale().multiply(0.05),
            ).getInfo()

            return (best["min1"], best["min2"], best["min"], True)
        else:
            # Position is already over land
            return (lon, lat, 0, False)

    def singleRequest(self, jsonObj, requestType):
        """
        Handle a single API request for timeseries extraction.

        Validates input parameters, processes the timeseries request, and returns
        results in standardized JSON format. Supports both time-bounded extraction
        and explicit pressure-based altitude computation.

        Args:
            jsonObj (dict): JSON request object with required parameters
            requestType: Request type (unused, maintained for compatibility)

        Returns:
            tuple: (status_code, headers, json_response)

        Required Parameters:
            - lon, lat: Coordinate location

        Time-bounded mode (requires):
            - startTime, endTime: UNIX timestamps for data range

        Pressure-based mode (requires):
            - time: Array of UNIX timestamps
            - pressure: Array of pressure measurements in Pascal
        """
        timeStamp = math.floor(datetime.datetime.utcnow().timestamp())

        # Validate required coordinate parameters
        if "lon" not in jsonObj.keys() or "lat" not in jsonObj.keys():
            return printErrorMessage(timeStamp, "Longitude and latitude are mandatory!")

        # Parse and validate longitude
        try:
            lon = float(jsonObj["lon"])
        except (ValueError, TypeError):
            return printErrorMessage(timeStamp, "Longitude is not a valid float number")

        # Parse and validate latitude
        try:
            lat = float(jsonObj["lat"])
        except (ValueError, TypeError):
            return printErrorMessage(timeStamp, "Latitude is not a valid float number")

        # Determine request mode based on available parameters
        informedTimeSeries = False
        if "time" in jsonObj.keys() and "pressure" in jsonObj.keys():
            informedTimeSeries = True
        else:
            if "startTime" not in jsonObj.keys() or "endTime" not in jsonObj.keys():
                return printErrorMessage(
                    timeStamp,
                    "Either (startTime AND endTime) OR (time AND pressure) arrays are mandatory!",
                )

        if informedTimeSeries:
            # Extract explicit time-pressure data
            time = jsonObj["time"]
            pressure = jsonObj["pressure"]
        else:
            # Parse time-bounded parameters
            try:
                timeStart = int(jsonObj["startTime"])
            except (ValueError, TypeError):
                return printErrorMessage(timeStamp, "StartTime is not a valid integer")

            try:
                timeEnd = int(jsonObj["endTime"])
            except (ValueError, TypeError):
                return printErrorMessage(timeStamp, "EndTime is not a valid integer")

            # Convert to milliseconds for Earth Engine
            timeStart = timeStart * 1000
            timeEnd = timeEnd * 1000

        try:
            # Validate and adjust coordinates if necessary
            lon, lat, dist, change = self.checkPosition([lon, lat])

            # Process request based on mode
            if informedTimeSeries:
                url = self.explicitTimeCollection(time, pressure, [lon, lat])
            else:
                url = self.boundingTimeCollection(timeStart, timeEnd, [lon, lat])

            # Prepare successful response
            response = {
                "status": "success",
                "taskID": timeStamp,
                "data": {
                    "format": "csv",
                    "url": url,
                    "lon": lon,
                    "lat": lat,
                    "distInter": dist,
                },
            }
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
