"""Choose the projected CRS a feature is measured in, and transform geometries into it.

Strategy (deliberately simple):
1. No source CRS          -> refuse. We never guess a CRS.
2. Projected source CRS   -> measure in it directly, converting its linear unit to metres.
   Exception: world-scale cylindrical projections (Mercator, Web Mercator, Plate Carrée) are
   "projected" but badly distort distance and area away from the equator, so they are
   treated like geographic input (step 3).
3. Geographic source CRS  -> find the feature's lon/lat extent, pick the WGS 84 UTM zone
   containing its centre (via pyproj's CRS database), and transform the geometry into it.
   Features UTM cannot represent well (polar, or wider than one zone) are refused with a reason.
"""

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info
from pyproj.exceptions import CRSError
from shapely.geometry.base import BaseGeometry

WGS84 = CRS.from_epsg(4326)

# UTM is defined between 80°S and 84°N; the poles use a different system (UPS).
UTM_MIN_LATITUDE = -80.0
UTM_MAX_LATITUDE = 84.0
# A UTM zone is 6° wide. Distortion grows quickly outside it, so wider features are not measured.
MAX_LONGITUDE_SPAN_DEGREES = 6.0


class CrsSelectionError(Exception):
    """No trustworthy projected CRS could be chosen. The message is user-facing."""


@dataclass(frozen=True)
class MeasurementCrs:
    crs: CRS
    label: str  # e.g. "EPSG:32643"
    metres_per_unit: float  # 1.0 for metre-based CRSs, 0.3048006 for US survey feet, ...


@lru_cache(maxsize=128)
def parse_crs(text: str) -> CRS:
    try:
        return CRS.from_user_input(text)
    except CRSError as exc:
        raise CrsSelectionError(f"Source CRS could not be interpreted: {exc}") from exc


@lru_cache(maxsize=128)
def _transformer(source: CRS, target: CRS) -> Transformer:
    # always_xy: treat coordinates as (lon, lat) / (x, y) regardless of the CRS's official axis order,
    # which matches how GDAL hands us Shapefile and KML coordinates.
    return Transformer.from_crs(source, target, always_xy=True)


def transform_geometry(geometry: BaseGeometry, source: CRS, target: CRS) -> BaseGeometry:
    if source == target:
        return geometry
    transformer = _transformer(source, target)
    return shapely.transform(
        geometry, lambda coords: np.column_stack(transformer.transform(coords[:, 0], coords[:, 1]))
    )


def select_measurement_crs(geometry: BaseGeometry, source: CRS | None) -> MeasurementCrs:
    if source is None:
        raise CrsSelectionError(
            "Source CRS is unknown (e.g. Shapefile without a .prj); a CRS is not assumed, so no measurement."
        )
    if source.is_projected and not _is_world_cylindrical(source):
        return MeasurementCrs(crs=source, label=crs_to_string(source), metres_per_unit=_metres_per_unit(source))
    if not (source.is_geographic or source.is_projected):
        raise CrsSelectionError(f"Unsupported source CRS type for measurement: {source.type_name}.")

    utm = utm_crs_for(geometry, source)
    return MeasurementCrs(crs=utm, label=crs_to_string(utm), metres_per_unit=1.0)


def utm_crs_for(geometry: BaseGeometry, source: CRS) -> CRS:
    """Pick the WGS 84 UTM zone containing the centre of the feature's lon/lat bounding box."""
    west, south, east, north = transform_geometry(geometry, source, WGS84).bounds
    if not all(map(math.isfinite, (west, south, east, north))) or not (
        -180 <= west <= east <= 180 and -90 <= south <= north <= 90
    ):
        raise CrsSelectionError(
            "Coordinates are outside valid longitude/latitude ranges for the declared CRS "
            "(the file's CRS may be mislabelled)."
        )
    if south < UTM_MIN_LATITUDE or north > UTM_MAX_LATITUDE:
        raise CrsSelectionError(
            f"Feature extends beyond UTM coverage ({UTM_MIN_LATITUDE:g}° to {UTM_MAX_LATITUDE:g}° latitude); "
            "polar features are not measured."
        )
    if east - west > 180:
        # A small feature crossing 180° has coordinates near both -180 and +180, so its lon/lat
        # bounding box spans almost the whole globe. Choosing a zone from that box would be wrong.
        raise CrsSelectionError(
            "Feature appears to cross the 180° meridian; UTM zone selection does not handle "
            "antimeridian-wrapping geometries, so it is not measured."
        )
    if east - west > MAX_LONGITUDE_SPAN_DEGREES:
        raise CrsSelectionError(
            f"Feature spans {east - west:.1f}° of longitude, wider than one UTM zone "
            f"({MAX_LONGITUDE_SPAN_DEGREES:g}°); UTM would distort it too much to measure reliably."
        )

    lon, lat = (west + east) / 2, (south + north) / 2
    zones = query_utm_crs_info(datum_name="WGS 84", area_of_interest=AreaOfInterest(lon, lat, lon, lat))
    if not zones:
        raise CrsSelectionError(f"No UTM zone found for location ({lon:.4f}, {lat:.4f}).")
    # On a zone boundary or the equator two zones match; both are equally valid, so pick deterministically.
    code = min(zones, key=lambda zone: zone.code).code
    return CRS.from_epsg(int(code))


def crs_to_string(crs: CRS) -> str:
    """'EPSG:4326' style when the CRS matches a known code, otherwise WKT (which parse_crs can read back)."""
    authority = crs.to_authority(min_confidence=70)
    return f"{authority[0]}:{authority[1]}" if authority else crs.to_wkt()


def _is_world_cylindrical(crs: CRS) -> bool:
    operation = crs.coordinate_operation
    method = operation.method_name.lower() if operation else ""
    is_mercator = "mercator" in method and "transverse" not in method
    return is_mercator or "equidistant cylindrical" in method


def _metres_per_unit(crs: CRS) -> float:
    axis = crs.axis_info[0] if crs.axis_info else None
    factor = axis.unit_conversion_factor if axis else None
    if not factor or axis.unit_name in {"degree", "radian", "grad"}:
        raise CrsSelectionError(f"Projected CRS {crs.name} has no usable linear unit.")
    return factor
