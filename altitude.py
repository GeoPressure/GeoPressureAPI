"""Shared Earth Engine altitude formula, matching GeoPressureR.

Justification: https://github.com/GeoPressure/altitude-validation
"""
import math

ALTITUDE_FORMULAS = ("virtual", "standard")
LAPSE_COEF = (-6.6306, 3.7168, -0.0744, 3.1974)
LAPSE_RANGE = (-9.5, -2.0)


def pressure_to_altitude(ee, pressure, image, elevation, latitude, date, formula="virtual"):
    """Convert pressure (Pa) to altitude (m) using matched ERA5 fields."""
    if formula == "virtual":
        td = image.select("dewpoint_temperature_2m").subtract(273.15)
        e = td.multiply(17.67).divide(td.add(243.5)).exp().multiply(611.2)
        temperature = image.expression(
            "T * (1 + 0.608 * 0.622 * e / (sp - 0.378 * e))",
            {"T": image.select("temperature_2m"), "sp": image.select("surface_pressure"), "e": e},
        )
        latitude = ee.Number(latitude)
        doy = ee.Date(date).getRelative("day", "year").add(1)
        sdoy = ee.Number(ee.Algorithms.If(latitude.lt(0), doy.add(182).mod(365).add(1), doy))
        a = latitude.abs().divide(90)
        c = sdoy.subtract(15).multiply(2 * math.pi / 365.25).cos()
        b1, b2, b3, b4 = LAPSE_COEF
        lapse = a.multiply(b2).add(b1).add(a.multiply(b4).add(b3).multiply(c)).clamp(*LAPSE_RANGE).divide(1000)
    else:
        temperature = image.select("temperature_2m")
        lapse = ee.Number(-0.0065)
    return temperature.divide(lapse).multiply(
        ee.Image.constant(ee.Number(pressure)).divide(image.select("surface_pressure"))
        .pow(lapse.multiply(-8.31432 / 9.80665 / 0.0289644)).subtract(1)
    ).add(elevation)
