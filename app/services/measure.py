"""Per-feature measurement: polygon area (m²) and line length (m).

Flow for one feature:
    classify geometry type -> drop Z -> repair invalid polygons -> choose projected CRS
    -> transform geometry -> area/length in projected units -> convert to metres.
Every path that does not produce a number returns a note explaining why.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass

import shapely
from pyproj.exceptions import ProjError
from shapely.errors import GEOSException
from shapely.geometry import MultiPolygon
from shapely.geometry.base import BaseGeometry

from app.models import MeasurementStatus, MeasurementType
from app.services.crs import CrsSelectionError, parse_crs, select_measurement_crs, transform_geometry

AREA_GEOMETRY_TYPES = frozenset({"Polygon", "MultiPolygon"})
LENGTH_GEOMETRY_TYPES = frozenset({"LineString", "MultiLineString", "LinearRing"})
POINT_GEOMETRY_TYPES = frozenset({"Point", "MultiPoint"})

AREA_UNIT = "m²"
LENGTH_UNIT = "m"


@dataclass(frozen=True)
class MeasurementResult:
    measurement_type: MeasurementType
    status: MeasurementStatus
    value: float | None = None
    unit: str | None = None
    projected_crs: str | None = None
    note: str | None = None


def measure_feature(geometry: BaseGeometry | None, source_crs: str | None) -> MeasurementResult:
    if geometry is None:
        return _unavailable(MeasurementType.NONE, "Feature has no geometry.")

    geometry_type = geometry.geom_type
    if geometry_type in POINT_GEOMETRY_TYPES:
        return MeasurementResult(
            MeasurementType.NONE, MeasurementStatus.NOT_APPLICABLE, note="Point geometries have no area or length."
        )
    if geometry_type in AREA_GEOMETRY_TYPES:
        kind = MeasurementType.AREA
    elif geometry_type in LENGTH_GEOMETRY_TYPES:
        kind = MeasurementType.LENGTH
    else:
        return _unavailable(MeasurementType.NONE, f"Geometry type '{geometry_type}' is not supported for measurement.")

    if geometry.is_empty:
        return _unavailable(kind, "Geometry is empty.")

    notes: list[str] = []
    # Measurements are planar (2D); elevation in KML/3D shapefiles is ignored.
    geometry = shapely.force_2d(geometry)

    if kind is MeasurementType.AREA and not geometry.is_valid:
        repaired, note = _repair_polygonal(geometry)
        notes.append(note)
        if repaired is None:
            return _unavailable(kind, *notes)
        geometry = repaired

    try:
        source = parse_crs(source_crs) if source_crs else None
        target = select_measurement_crs(geometry, source)
        projected = transform_geometry(geometry, source, target.crs)
    except CrsSelectionError as exc:
        return _unavailable(kind, *notes, str(exc))
    except (ProjError, GEOSException, ValueError) as exc:
        return _unavailable(kind, *notes, f"Could not transform geometry to a projected CRS: {exc}")

    if kind is MeasurementType.AREA:
        value, unit = projected.area * target.metres_per_unit**2, AREA_UNIT
    else:
        value, unit = projected.length * target.metres_per_unit, LENGTH_UNIT

    if not math.isfinite(value):
        return _unavailable(kind, *notes, "Measurement produced a non-finite value after projection.")

    return MeasurementResult(
        measurement_type=kind,
        status=MeasurementStatus.MEASURED,
        value=value,
        unit=unit,
        projected_crs=target.label,
        note="; ".join(notes) or None,
    )


def _repair_polygonal(geometry: BaseGeometry) -> tuple[BaseGeometry | None, str]:
    """Repair an invalid (Multi)Polygon with GEOS make_valid, keeping only its polygonal part.

    The 'structure' method rebuilds polygons from their rings (e.g. a self-intersecting
    'bowtie' becomes two triangles) and always yields a polygonal result.
    """
    reason = shapely.is_valid_reason(geometry)
    try:
        repaired = shapely.make_valid(geometry, method="structure", keep_collapsed=False)
    except GEOSException as exc:
        return None, f"Geometry is invalid ({reason}) and could not be repaired: {exc}."

    if repaired.geom_type == "GeometryCollection":
        polygons = [part for part in repaired.geoms if part.geom_type in AREA_GEOMETRY_TYPES]
        repaired = MultiPolygon([p for part in polygons for p in getattr(part, "geoms", [part])])

    if repaired.is_empty or repaired.geom_type not in AREA_GEOMETRY_TYPES:
        return None, f"Geometry is invalid ({reason}) and repairing it left no area to measure."
    return repaired, f"Geometry was invalid ({reason}); measured after repairing it with make_valid."


def _unavailable(kind: MeasurementType, *notes: str) -> MeasurementResult:
    return MeasurementResult(kind, MeasurementStatus.UNAVAILABLE, note="; ".join(notes))


@dataclass(frozen=True)
class MeasurementSummary:
    feature_count: int
    measured_count: int
    not_applicable_count: int
    unavailable_count: int
    total_area_m2: float | None
    total_length_m: float | None
    totals_complete: bool
    note: str | None


def summarize(results: Iterable[tuple[MeasurementType, MeasurementStatus, float | None]]) -> MeasurementSummary:
    """Totals per quantity, or None when an unmeasured feature could have contributed to it.

    An unmeasured polygon makes the area total unknown; an unmeasured feature of unsupported
    type (NONE) could have contributed to either, so it blocks both totals.
    """
    results = list(results)
    count = {status: sum(1 for _, s, _ in results if s is status) for status in MeasurementStatus}
    unavailable_types = {kind for kind, status, _ in results if status is MeasurementStatus.UNAVAILABLE}

    def total(kind: MeasurementType) -> float | None:
        if unavailable_types & {kind, MeasurementType.NONE}:
            return None
        return sum(v for k, s, v in results if k is kind and s is MeasurementStatus.MEASURED and v is not None)

    unavailable = count[MeasurementStatus.UNAVAILABLE]
    return MeasurementSummary(
        feature_count=len(results),
        measured_count=count[MeasurementStatus.MEASURED],
        not_applicable_count=count[MeasurementStatus.NOT_APPLICABLE],
        unavailable_count=unavailable,
        total_area_m2=total(MeasurementType.AREA),
        total_length_m=total(MeasurementType.LENGTH),
        totals_complete=unavailable == 0,
        note=(
            f"{unavailable} feature(s) could not be measured; totals they could affect are null. "
            "See each feature's note."
            if unavailable
            else None
        ),
    )
