"""
GeoPressure API - Pressure Path Cloud Function

This module provides a Google Cloud Function endpoint for extracting atmospheric
pressure and meteorological variables from ERA5/ERA5-LAND reanalysis data along
specified paths with optional altitude computation.

Author: GeoPressure Team
"""

import functions_framework
import json
import os
import tempfile

from pressurePath import *

# Configuration
highVolumeEndpoint = True  # Fixed typo: was "hightVolumeEndpoint"

# Load Google Earth Engine credentials from environment variables
gee_api_key_content = (
    os.environ.get("GEE_API_KEY").encode("utf-8").decode("unicode_escape")
)
gee_api_address = (
    os.environ.get("GEE_API_ADDRESS").encode("utf-8").decode("unicode_escape")
)

# Prepare the JSON content as a dictionary for GEE authentication
key_data = {
    "private_key": gee_api_key_content,
    "client_email": gee_api_address,
    "token_uri": "https://oauth2.googleapis.com/token",
}

# Create a temporary JSON file with the key content for GEE initialization
with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp_file:
    json.dump(key_data, tmp_file)
    tmp_file.flush()
    # Initialize the GeoPressure pressure path processor
    process = GP_pressurePath(gee_api_address, tmp_file.name, highVolumeEndpoint)


@functions_framework.http
def pressurePath(request):
    """
    Google Cloud Function entry point for pressure path analysis API.

    Handles HTTP requests for extracting atmospheric variables from ERA5/ERA5-LAND
    data along specified paths with optional altitude computation using the
    barometric formula.

    Args:
        request: Flask request object containing JSON parameters

    Returns:
        tuple: (response_body, status_code, headers)

    Request Parameters (JSON):
        Required:
        - path OR (lat AND lon): Coordinate specification
        - time: Array of UNIX timestamps
        - variable: Array of ERA5 variable names to extract

        Optional:
        - pressure: Geolocator pressure measurements for altitude computation
        - dataset: "single-levels" (default), "land", or "both". "land" and "both"
          must not be used when altitude is needed -- ERA5-LAND surface_pressure is
          not hydrostatically consistent with ERA5-LAND orography.
        - altitudeFormula: "standard" (default; 2 m temperature, -6.5 K/km, as
          GeoPressureR) or "virtual" (2 m virtual temperature and a lapse rate varying
          with season and latitude; more accurate in flight)
        - workers: Number of processing chunks (default: 10)

    Response Format:
        Success: {"status": "success", "taskID": timestamp, "data": {...}}
                 plus "warning" when altitude was computed from "land"/"both"
        Error: {"status": "error", "taskID": timestamp, "errorMessage": "...", "advice": "..."}
    """
    # Default CORS headers for cross-origin requests
    default_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
        "Access-Control-Allow-Headers": "Accept, Content-Type",
    }

    # Handle preflight OPTIONS request for CORS
    if request.method == "OPTIONS":
        return ("", 204, default_headers)

    # Process POST requests with JSON data
    if request.method == "POST":
        request_json = request.get_json(silent=True)
        if request_json:
            try:
                # Process the pressure path request
                code, type_response, response = process.singleRequest(
                    request_json, None
                )
                return (response, code, {**default_headers, **type_response})
            except Exception as e:
                print(f"Catastrophic error in pressure path processing: {e}")
                import traceback

                traceback.print_exc()
                return (
                    "Catastrophic Error: An unexpected error occurred during processing.".encode(
                        "utf-8"
                    ),
                    500,
                    default_headers,
                )
        else:
            return (
                "Your input must be in JSON format over a POST request. It appears you're using API V2. "
                "If you intended to use API V1 (which is no longer maintained), you may do so. However, "
                "we strongly recommend using a POST request with JSON on the API V2 for better support.".encode(
                    "utf-8"
                ),  # Fixed typo: was "PSOT"
                400,  # Changed from 500 to 400 (Bad Request)
                default_headers,
            )
    else:
        # Handle unsupported HTTP methods
        return (
            {
                "error": "Method Not Allowed",
                "message": "This endpoint only supports POST requests.",
            },
            405,
            default_headers,
        )
