#!/usr/local/bin/python3
"""
GeoPressure API - Map Module

This module provides functionality to generate probability maps for geolocator analysis
using Mean Squared Error (MSE) calculations between observed pressure data and ERA5
atmospheric reanalysis. It supports spatial probability mapping with altitude constraints
and uncertainty quantification.

Author: GeoPressure Team
"""

# import os
# import logging
# import http.client as http_client

# http_client.HTTPConnection.debuglevel = 1

# logging.basicConfig(level=logging.DEBUG)

# logging.getLogger("urllib3").setLevel(logging.DEBUG)
# logging.getLogger("urllib3").propagate = True
# logging.getLogger("requests").setLevel(logging.DEBUG)
# logging.getLogger("google.auth").setLevel(logging.DEBUG)
# logging.getLogger("google.api_core").setLevel(logging.DEBUG)

import datetime
import json
import math
import os
import time as tm
from concurrent.futures import ThreadPoolExecutor

import numpy

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


class GP_map_v2(GEE_Service):
    """
    GeoPressure Map Generation Class
    
    Generates probability maps for geolocator analysis using Mean Squared Error (MSE)
    calculations between observed pressure measurements and ERA5 atmospheric data.
    Includes altitude constraints and spatial uncertainty quantification.
    
    Features:
    - MSE-based probability mapping
    - ERA5-LAND atmospheric data integration
    - Altitude constraint masking using DEM data
    - Parallel processing for multiple labels
    - GeoTIFF export functionality
    - Configurable spatial resolution and sampling
    """

    def __init__(self, service_account, apiKeyFile, highvolume=False):
        """
        Initialize the GP_map_v2 service.
        
        Args:
            service_account (str): Google Earth Engine service account email
            apiKeyFile (str): Path to service account JSON key file
            highvolume (bool): Use high-volume Earth Engine endpoint
        """
        super(GP_map_v2, self).__init__(service_account, apiKeyFile, highvolume)
        
        # Get the last available timestamp for ERA5 data to validate requests
        self.endERA5 = 1
       # Load reference geopotential and DEM data
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

        # Rename hourly suffix to avoid band conflicts
        strToRemove = self.ee.String("_hourly")

        def removeHourly(str_):
            return self.ee.String(str_).slice(0, strToRemove.length().multiply(-1))

        oldNName = era5_land.first().bandNames().filter(
            self.ee.Filter.stringEndsWith('item', strToRemove)
        )
        newName = oldNName.map(removeHourly)

        def addRenamed(im):
            return im.addBands(im.select(oldNName, newName), None, True)

        era5_land = era5_land.map(addRenamed)

        # Join ERA5 and ERA5-LAND
        indexFilter = self.ee.Filter.equals(leftField="system:index", rightField="system:index")
        simpleJoin = self.ee.Join.saveFirst("match")
        simpleJoined = simpleJoin.apply(era5_single, era5_land, indexFilter)

        def combineBands(image):
            land_image = self.ee.Image(image.get("match"))
            commonBands = land_image.bandNames().filter(
                self.ee.Filter.inList("item", image.bandNames())
            )
            return image.addBands(
                self.ee.ImageCollection([image, land_image]).select(commonBands).mosaic(),
                None,
                True,
            )

        self.ERA5Combined = self.ee.ImageCollection(simpleJoined).map(combineBands)
        self.era5_land = era5_land
        self.era5_single = era5_single

        # Keep the endERA5 timestamp check
        self.endERA5 = (
            self.era5_land.filterDate("2024", "2100")
            .aggregate_max("system:time_start")
            .getInfo()
        )


    def getMSE_Map(
        self,
        time,
        pressure,
        label,
        W,
        S,
        E,
        N,
        boxSize,
        scaleFactor=10,  # Fixed typo: was "sclaeFcator"
        includeMask=True,
        maxSample=250,
        margin=30,
        maskThreshold=0.9,
        dataset="single-levels"
    ):
        """
        Generate MSE-based probability maps for geolocator analysis.
        
        This method computes Mean Squared Error maps by comparing observed pressure
        measurements with ERA5 atmospheric data, accounting for altitude constraints
        and temporal matching. Uses barometric formula for altitude calculations.
        
        Args:
            time (list): UNIX timestamps for pressure measurements
            pressure (list): Observed pressure values in Pascal
            label (list): Labels for grouping measurements
            W, S, E, N (float): Bounding box coordinates (West, South, East, North)
            boxSize (list): Output image dimensions [width, height] in pixels
            scaleFactor (float): DEM resolution scale factor (default: 10)
            includeMask (bool): Include altitude probability mask in output
            maxSample (int): Maximum samples per label for processing (default: 250)
            margin (float): Altitude tolerance margin in meters (default: 30)
            maskThreshold (float): Probability threshold for masking (default: 0.9)
            
        Returns:
            dict: Contains GeoTIFF URLs, metadata, and processing information
            
        Algorithm:
        1. Group measurements by label and sample randomly if needed
        2. Match measurements with ERA5 data by timestamp (±1 hour tolerance)
        3. Calculate pressure differences and altitude constraints
        4. Generate MSE maps with optional altitude masking
        5. Export as GeoTIFF downloads
        """

        def makeFeature(measurement_list):
            """
            Convert measurement list to Earth Engine Feature.
            
            Args:
                measurement_list: [time, pressure, label] triplet
                
            Returns:
                Earth Engine Feature with timestamp, pressure, and label properties
            """
            measurement_list = self.ee.List(measurement_list)
            return self.ee.Feature(
                None,
                {
                    "system:time_start": self.ee.Number(measurement_list.get(0)).multiply(1000),
                    "pressure": measurement_list.get(1),
                    "label": measurement_list.get(2),
                },
            )

        # Combine input arrays into measurement triplets
        py_collection = list(zip(time, pressure, label))

        def runMSEmatch(labelFeature):
            """
            Compute MSE map for a specific label group.
            
            This function implements the core algorithm for pressure-based probability
            mapping using the barometric formula and ERA5 atmospheric data.
            
            Args:
                labelFeature: Earth Engine FeatureCollection for one label
                
            Returns:
                Earth Engine Image with MSE and altitude probability bands
            """
            
            # Physical constants for barometric formula
            # Standard temperature lapse rate [K/m]
            Lb = -0.0065
            # Universal gas constant [J/mol/K]
            R = 8.31432
            # Gravitational acceleration [m/s²]
            g0 = 9.80665
            # Molar mass of Earth's air [kg/mol]
            M = 0.0289644
            # Standard sea level temperature [K]
            T0 = 273.15 + 15

            # Randomly sample measurements if too many (computational efficiency)
            labelFeature = (
                labelFeature.randomColumn("random").sort("random").limit(maxSample)
            )

            # Calculate mean pressure and time range for this label
            pressureMeanSensor = labelFeature.aggregate_mean("pressure")  # Fixed typo: was "presureMeanSesnor"
            start = labelFeature.aggregate_min("system:time_start")
            end = labelFeature.aggregate_max("system:time_start")

            # Select dataset dynamically
            if dataset.lower() == "single-levels":
                ERA5 = self.era5_single
            elif dataset.lower() == "land":
                ERA5 = self.era5_land
            else:
                ERA5 = self.ERA5Combined

            # Filter ERA5 data by time range (±1 hour buffer)
            ERA5_pressure = ERA5.filterDate(  # Fixed typo: was "ERA5_pressur"
                self.ee.Date(start).advance(-1, "hour"),
                self.ee.Date(end).advance(1, "hour"),
            ).select(["surface_pressure", "temperature_2m","geopotential"])

            # Match each measurement with closest ERA5 timestamp
            era5_labelFeature = self.ee.Join.saveBest(  # Fixed typo: was "era5_llabelFeature"
                matchKey="bestERA5", measureKey="diff"
            ).apply(
                labelFeature,
                ERA5_pressure,
                self.ee.Filter.maxDifference(
                    3600 * 1000,  # 1 hour tolerance in milliseconds
                    leftField="system:time_start",
                    rightField="system:time_start",
                ),
            )

            def getPressureMap(feature):  # Fixed typo: was "getpresurMap"
                """Extract ERA5 pressure map from matched feature."""
                return self.ee.Image(feature.get("bestERA5"))

            # Calculate mean pressure map across all measurements
            meanMapPressure = (
                self.ee.ImageCollection(era5_labelFeature.map(getPressureMap))
                .select("surface_pressure")
                .mean()
            )

            def getError(feature):
                """
                Calculate MSE and altitude probability for each measurement.

                Uses barometric formula to compute altitude from pressure difference
                and checks against DEM constraints for spatial feasibility.
                """
                
                # Calculate pressure error (observed vs ERA5)
                error = (
                    self.ee.Image(feature.get("bestERA5"))
                    .select("surface_pressure")
                    .subtract(meanMapPressure)
                    .subtract(
                        self.ee.Number(feature.get("pressure")).subtract(pressureMeanSensor)
                    )
                    .toFloat()
                )

                # Load reference geopotential and DEM data
                im = self.ee.Image(feature.get("bestERA5"))
                geoPot = im.select("geopotential").divide(g0)  # Convert geopotential to altitude [m]

                altIm = (
                    self.ee.ImageCollection(
                        "projects/earthimages4unil/assets/PostDocProjects/rafnuss/GLO_minMax_reduced"
                    )
                    .filter(self.ee.Filter.gte("factor", scaleFactor))
                    .sort("factor")
                    .first()
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
                            self.ee.Image(feature.get("bestERA5")).select("surface_pressure")
                        )
                        .pow(-R * Lb / g0 / M)
                        .subtract(1)
                    )
                    .add(geoPot)
                )

                # Check altitude feasibility against DEM constraints
                isPossible = (
                    dh.gte(altIm.select("DEM_min").add(-margin))
                    .And(dh.lte(altIm.select("DEM_max").add(margin)))
                    .toFloat()
                )

                return error.multiply(error).addBands(isPossible).rename(["mse", "probAlt"])

            # Calculate MSE map across all measurements for this label
            aggregatedMap = self.ee.ImageCollection(era5_labelFeature.map(getError)).mean()  # Fixed typo: was "agregatedMap"

            # Apply altitude probability masking if threshold specified
            if maskThreshold > 0:
                aggregatedMap = aggregatedMap.addBands(
                    aggregatedMap.select("mse")
                    .updateMask(aggregatedMap.select("probAlt").gte(maskThreshold))
                    .unmask(-1),
                    None,
                    True,
                )

            # Remove altitude band if not requested in output
            if not includeMask:
                aggregatedMap = aggregatedMap.select("mse")

            # Apply ERA5 data mask and fill missing areas
            aggregatedMap = aggregatedMap.updateMask(
                ERA5_pressure.first().select(0).mask()
            ).unmask(-2)

            return aggregatedMap.set("label", labelFeature.get("label"))

        # Get unique labels for processing
        listLabel_py = list(set(label))
        urls = {}
        ims = {}

        def process_label(label_id):
            """
            Process one label group in parallel.
            
            Args:
                label_id: Label identifier to process
                
            Returns:
                Tuple of (label_id, computed_image)
            """
            feature_collection = self.ee.FeatureCollection(
                self.ee.List(
                    list(filter(lambda x: x[2] == label_id, py_collection))
                ).map(makeFeature)
            )
            return label_id, runMSEmatch(feature_collection)

        # Process all labels in parallel (single worker to avoid EE quota issues)
        with ThreadPoolExecutor(max_workers=1) as executor:
            results = executor.map(process_label, listLabel_py)

        # Collect processing results
        for label_id, result in results:
            ims[label_id] = result

        # Define bounding box for export
        bbox = self.ee.Algorithms.GeometryConstructors.BBox(W, S, E, N)

        def getEEUrl(label_id):
            """
            Generate download URL for processed map.
            
            Args:
                label_id: Label to generate URL for
            """
            start_time = tm.time()
            if ims[label_id]:
                try:
                    urls[label_id] = ims[label_id].getDownloadURL(
                        {
                            "name": "label",
                            "dimensions": boxSize,
                            "format": "GEO_TIFF",
                            "region": bbox,
                        }
                    )
                except Exception as e:
                    print(f"Error generating URL for label {label_id}: {e}")
                    urls[label_id] = None
            else:
                urls[label_id] = None
            end_time = tm.time()

        # Generate download URLs in parallel
        start_time = tm.time()
        with ThreadPoolExecutor(max_workers=numCores * 3) as executor:
            executor.map(getEEUrl, listLabel_py)
        end_time = tm.time()

        return {
            "format": "GEOTIFF",
            "labels": listLabel_py,
            "urls": [urls[label_id] for label_id in listLabel_py],
            "resolution": 1 / scaleFactor,
            "bbox": {"W": W, "S": S, "E": E, "N": N},
            "size": boxSize,
            "time2GetUrls": end_time - start_time,
            "includeMask": includeMask,
            "maskThreshold": maskThreshold,
        }

    def singleRequest(self, jsonObj, requestType):
        """
        Handle a single API request for MSE map generation.
        
        Validates input parameters, processes the map generation request,
        and returns results in standardized JSON format. Supports bounding box
        specification and various configuration options for map generation.
        
        Args:
            jsonObj (dict): JSON request object with required parameters
            requestType: Request type (unused, maintained for compatibility)
            
        Returns:
            tuple: (status_code, headers, json_response)
            
        Required Parameters:
            - W, S, E, N: Bounding box coordinates (West, South, East, North)
            - time: Array of UNIX timestamps
            - pressure: Array of pressure measurements in Pascal
            - label: Array of labels for grouping measurements
            
        Optional Parameters:
            - scale: DEM resolution scale factor (default: 10)
            - maxSample: Maximum samples per label (default: 250)
            - margin: Altitude tolerance in meters (default: 30)
            - includeMask: Include altitude probability mask (default: True)
            - maskThreshold: Probability threshold for masking (default: 0.9)
        """
        timeStamp = math.floor(datetime.datetime.utcnow().timestamp())

        # Validate JSON object
        if len(jsonObj.keys()) < 1:
            return printErrorMessage(
                timeStamp, "JSON object is empty! Did you send it as JSON by accident?"
            )

        # Validate required bounding box parameters
        if (
            "W" not in jsonObj.keys()
            or "S" not in jsonObj.keys()
            or "E" not in jsonObj.keys()
            or "N" not in jsonObj.keys()
        ):
            return printErrorMessage(
                timeStamp, "W, S, E or N are missing. The bounding box is mandatory."
            )

        # Validate required data arrays
        if (
            "time" not in jsonObj.keys()
            or "pressure" not in jsonObj.keys()
            or "label" not in jsonObj.keys()
        ):
            return printErrorMessage(
                timeStamp, "Time, pressure and label arrays are mandatory."
            )

        # Parse and validate bounding box coordinates
        try:
            W = float(jsonObj["W"])
        except (ValueError, TypeError):
            return printErrorMessage(timeStamp, "W is not a valid float number.")

        try:
            S = float(jsonObj["S"])
        except (ValueError, TypeError):
            return printErrorMessage(timeStamp, "S is not a valid float number.")

        try:
            E = float(jsonObj["E"])
        except (ValueError, TypeError):
            return printErrorMessage(timeStamp, "E is not a valid float number.")

        try:
            N = float(jsonObj["N"])
        except (ValueError, TypeError):
            return printErrorMessage(timeStamp, "N is not a valid float number.")

        # Parse optional scale parameter
        scale = 10
        if "scale" in jsonObj.keys():
            try:
                scale = float(jsonObj["scale"])
            except (ValueError, TypeError):
                return printErrorMessage(timeStamp, "Scale should be a number.")

        # Parse optional maxSample parameter
        maxSample = 250
        if "maxSample" in jsonObj.keys():
            try:
                maxSample = int(jsonObj["maxSample"])
            except (ValueError, TypeError):
                return printErrorMessage(timeStamp, "MaxSample should be an integer.")

        # Parse optional margin parameter
        margin = 30
        if "margin" in jsonObj.keys():
            try:
                margin = float(jsonObj["margin"])
            except (ValueError, TypeError):
                return printErrorMessage(timeStamp, "Margin is not a valid float number.")

        # Parse optional includeMask parameter
        includeMask = True
        if "includeMask" in jsonObj.keys():
            includeMask = jsonObj["includeMask"]

        # Parse optional maskThreshold parameter
        maskThreshold = 0.9
        if "maskThreshold" in jsonObj.keys():
            maskThreshold = jsonObj["maskThreshold"]

        # Calculate output image dimensions
        sizeLon = (E - W) * scale
        sizeLat = (N - S) * scale

        # Validate that dimensions result in integer pixel counts
        if math.fabs(sizeLon - round(sizeLon)) > 0.001:
            return printErrorMessage(
                timeStamp, "(E-W)*scale should result in an integer pixel count."
            )

        if math.fabs(sizeLat - round(sizeLat)) > 0.001:
            return printErrorMessage(
                timeStamp, "(N-S)*scale should result in an integer pixel count."
            )

        sizeLon = round(sizeLon)
        sizeLat = round(sizeLat)

        # Extract data arrays
        time = jsonObj["time"]
        pressure = jsonObj["pressure"]
        label = jsonObj["label"]

        # Optional dataset selector ("land", "single-levels", or "both")
        dataset = "single-levels"
        if "dataset" in jsonObj.keys():
            dataset_val = jsonObj["dataset"]
            if isinstance(dataset_val, list) and len(dataset_val) > 0:
                dataset = str(dataset_val[0])
            else:
                dataset = str(dataset_val)

        # Validate array lengths
        if len(time) != len(pressure) or len(time) != len(label):
            return printErrorMessage(
                timeStamp, "Pressure, time and label arrays must have the same length."
            )

        try:
            # Check if requested time range is within ERA5 data availability
            if numpy.array(time).max() * 1000 > self.endERA5:
                return (
                    416,
                    {"Content-type": "application/json"},
                    json.JSONEncoder().encode(
                        {
                            "status": "error",
                            "taskID": timeStamp,
                            "errorMessage": "ERA5 data not available from {}. Request only pressure with earlier date.".format(
                                datetime.datetime.utcfromtimestamp(self.endERA5 / 1000)
                            ),
                            "lastERA5": self.endERA5,
                        }
                    ),
                )

            # Process the map generation request
            data = self.getMSE_Map(
                time,
                pressure,
                label,
                W,
                S,
                E,
                N,
                [sizeLon, sizeLat],
                scale,
                includeMask,
                maxSample,
                margin,
                maskThreshold,
                dataset,
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
