#!/usr/local/bin/python3
"""
GeoPressure API - Elevation Path Module

This module provides functionality to extract elevation profiles along specified paths
using Google Earth Engine's Digital Elevation Models (DEM). It supports customizable
sampling scales, percentile calculations, and path analysis for geolocator studies.

Author: GeoPressure Team
"""

import datetime
import json
import math

from GEE_API_server import GEE_Service


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


class GP_elevationPath(GEE_Service):
    """
    GeoPressure Elevation Path Analysis Class

    Extracts elevation profiles along specified paths using Digital Elevation Models.
    Supports multi-scale DEM data with percentile-based elevation statistics for
    uncertainty quantification in geolocator movement analysis.

    Features:
    - Custom DEM collection with multiple resolution factors
    - Path sampling at configurable distances
    - Percentile-based elevation statistics
    - Coordinate transformation and projection handling
    """

    def __init__(self, service_account, apiKeyFile, highvolume=False):
        """
        Initialize the GP_elevationPath service.

        Args:
            service_account (str): Google Earth Engine service account email
            apiKeyFile (str): Path to service account JSON key file
            highvolume (bool): Use high-volume Earth Engine endpoint
        """
        super(GP_elevationPath, self).__init__(service_account, apiKeyFile, highvolume)

    def getElevationAlongPath(self, path, distanceSampling, dataScale, percentileArray):
        """
        Extract elevation profiles along a specified path using DEM data.

        This method samples elevation data along a path at regular intervals,
        computing percentile statistics to account for DEM uncertainty and
        provide confidence intervals for elevation estimates.

        Args:
            path (list): List of [lon, lat] coordinate pairs defining the path
            distanceSampling (float): Distance between sampling points in meters
            dataScale (float): Spatial resolution of DEM data in meters
            percentileArray (list): Percentiles to compute (e.g., [10, 50, 90])

        Returns:
            dict: Contains elevation data arrays, scales, and metadata

        Algorithm:
        1. Convert path to line segments with distance calculations
        2. Sample points along path at specified intervals
        3. Extract DEM elevations at sample points
        4. Return percentile statistics and coordinates
        """

        # Store original scale for DEM selection
        imageScale = dataScale

        # Convert from meters to degrees (approximate conversion at equator)
        # 111139 meters ≈ 1 degree latitude/longitude
        dataScale = 111139 / dataScale
        distanceSampling = 111139 / distanceSampling

        # Select appropriate DEM from multi-resolution collection
        # Uses custom DEM collection with different resolution factors
        dem_collection = self.ee.ImageCollection(
            "projects/earthimages4unil/assets/PostDocProjects/rafnuss/GLO_reduced"
        )
        # A scale finer than the finest layer would select nothing (null image); use the finest
        imageScale = self.ee.Number(imageScale).min(dem_collection.aggregate_max("factor"))
        elevation_image = (
            dem_collection.filter(self.ee.Filter.gte("factor", imageScale))
            .sort("factor")
            .first()
        )

        # Debug: Print selected DEM info (remove in production)
        print(elevation_image.getInfo())

        # Convert path to Earth Engine LineString and extract coordinates
        line_points = self.ee.Geometry.LineString(path).coordinates()

        def polylineAsMultiline(idVal):
            """
            Convert path segments into individual line features with distances.

            Args:
                idVal: Index of the line segment

            Returns:
                Earth Engine Feature representing one path segment
            """
            idVal = self.ee.Number(idVal)
            segment = self.ee.Feature(
                self.ee.Geometry.LineString(
                    [line_points.get(idVal), line_points.get(idVal.add(1))],
                    "EPSG:4326",
                    True,
                ),
                {"startIndex": idVal},
            )
            return segment.set("dist", segment.length())

        # Create line segments from consecutive coordinate pairs
        line_indices = self.ee.List.sequence(0, line_points.size().subtract(2))
        line_segments = self.ee.FeatureCollection(line_indices.map(polylineAsMultiline))

        def lineAsCollection(line):
            """
            Sample points along a line segment at regular intervals.

            Args:
                line: Earth Engine Feature representing a path segment

            Returns:
                FeatureCollection of sample points with position metadata
            """
            length = line.length()
            start = self.ee.Geometry.Point(line.geometry().coordinates().get(0))

            # Calculate cumulative distance from start of path
            cumulative_distance = line_segments.filterMetadata(
                "startIndex", "less_than", line.getNumber("startIndex")
            ).aggregate_sum("dist")

            def getSampleFeatures(segment_geometry):
                """Extract sample point with position and distance metadata."""
                point = self.ee.Geometry.Point(
                    self.ee.Geometry(segment_geometry).coordinates().get(0)
                )
                return self.ee.Feature(
                    point,
                    {
                        "stapId": (
                            point.distance(start)
                            .divide(length)
                            .add(line.getNumber("startIndex"))
                        ),
                        "distance": point.distance(start).add(
                            cumulative_distance.round()
                        ),
                    },
                )

            # Cut line into segments at sampling intervals and extract points
            return self.ee.FeatureCollection(
                line.geometry()
                .cutLines(
                    self.ee.List.sequence(
                        0, length.add(distanceSampling), distanceSampling
                    )
                )
                .geometries()
                .map(getSampleFeatures)
            )

        # Generate all sample points along the path
        sample_points = line_segments.map(lineAsCollection).flatten()

        # Sample elevation data at all points
        # Add longitude/latitude bands for coordinate extraction
        elevation_collection = self.ee.FeatureCollection(
            elevation_image.round()
            .unmask(0)  # Fill missing values with 0
            .addBands(
                self.ee.Image.pixelLonLat()
                .multiply(1000)
                .round()
                .divide(1000)  # Round coordinates to 3 decimal places
            )
            # Sample in the DEM layer's own grid. Passing `dataScale` (111139 / scale m) made Earth
            # Engine build a grid of 111139 / 111319.49 = 0.99838 of the layer's cell size, anchored
            # at (-180, 90), which drifts away from the DEM cells (GeoPressureAPI#33).
            .reduceRegions(
                collection=sample_points,
                reducer=self.ee.Reducer.first(),
                crs=elevation_image.projection(),
            )
        )

        def toJson(key, val):
            """Extract array values for a given variable."""
            return elevation_collection.aggregate_array(val)

        # Create output dictionary mapping display names to data arrays
        output_keys = ["stapId", "lon", "lat", "distance"] + [
            f"{num}" for num in percentileArray
        ]
        data_keys = ["stapId", "longitude", "latitude", "distance"] + [
            f"DEM_p{num}" for num in percentileArray
        ]

        elevation_data = self.ee.Dictionary.fromLists(output_keys, data_keys).map(
            toJson
        )

        return {
            "percentileData": elevation_data.getInfo(),
            "scale": dataScale,
            "samplingScale": distanceSampling,
            "percentile": percentileArray,
        }

    def singleRequest(self, jsonObj, requestType):
        """
        Handle a single API request for elevation path analysis.

        Validates input parameters, processes the elevation extraction request,
        and returns results in standardized JSON format. Supports both path arrays
        and separate lat/lon arrays for coordinate specification.

        Args:
            jsonObj (dict): JSON request object with required parameters
            requestType: Request type (unused, maintained for compatibility)

        Returns:
            tuple: (status_code, headers, json_response)

        Required Parameters:
            - path OR (lat AND lon): Coordinate specification
            - scale: DEM resolution in meters
            - samplingScale: Distance between sample points in meters

        Optional Parameters:
            - percentile: Array of percentiles to compute (default: [10, 50, 90])
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

        # Validate required scale parameter
        if "scale" not in jsonObj.keys():
            return printErrorMessage(
                timeStamp, "Scale parameter is missing and required."
            )

        # Validate required samplingScale parameter
        if "samplingScale" not in jsonObj.keys():
            return printErrorMessage(
                timeStamp, "SamplingScale parameter is missing and required."
            )

        # Process coordinate input - convert lat/lon arrays to path if needed
        if "lat" in jsonObj.keys() and "lon" in jsonObj.keys():
            path = list(zip(jsonObj["lon"], jsonObj["lat"]))
            # Duplicate last point for path completion (original behavior)
            path = path + path[-1:]
        elif "path" in jsonObj.keys():
            path = jsonObj["path"]

        # Validate and parse scale parameter
        try:
            scale = float(jsonObj["scale"])
            samplingScale = scale  # Default sampling scale to DEM scale
        except (ValueError, TypeError):
            return printErrorMessage(timeStamp, "Scale should be a number.")

        # Parse optional samplingScale parameter
        if "samplingScale" in jsonObj.keys():
            try:
                samplingScale = float(jsonObj["samplingScale"])
            except (ValueError, TypeError):
                return printErrorMessage(timeStamp, "SamplingScale should be a number.")

        # Set default percentiles for elevation statistics
        percentile = [10, 50, 90]

        # Parse optional percentile parameter
        if "percentile" in jsonObj.keys():
            try:
                percentile = jsonObj["percentile"]
                # Validate percentile values are within valid range
                if not all(0 <= p <= 100 for p in percentile):
                    return printErrorMessage(
                        timeStamp, "Percentile values should be between 0 and 100."
                    )
            except (ValueError, TypeError):
                return printErrorMessage(
                    timeStamp, "Percentile should be an array of numbers."
                )

        try:
            # Process the elevation extraction request
            data = self.getElevationAlongPath(path, samplingScale, scale, percentile)
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
