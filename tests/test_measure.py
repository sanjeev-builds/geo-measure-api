"""Measurement correctness.

Expected values come from pyproj.Geod (geodesic calculations on the WGS84 ellipsoid), an
independent method that never uses UTM, so these tests check correctness, not self-consistency.
UTM's scale distortion within a zone is up to ~0.1% in length and ~0.2% in area, hence the tolerances.
"""

import numpy as np
import pytest
import shapely
from pyproj import Geod, Transformer
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
)
from shapely.geometry.polygon import orient

from app.models import MeasurementStatus, MeasurementType
from app.services.measure import measure_feature, summarize
from tests.factories import BANGALORE_SQUARE

GEOD = Geod(ellps="WGS84")
AREA_TOLERANCE = 0.005  # 0.5 %
LENGTH_TOLERANCE = 0.002  # 0.2 %

ROAD = LineString([(77.59, 12.97), (77.61, 12.99)])


def geodesic_area(geometry) -> float:
    # Geod uses ring winding to decide whether a ring adds or subtracts area, so normalise it first
    # (exterior counter-clockwise, holes clockwise).
    if geometry.geom_type == "Polygon":
        geometry = orient(geometry)
    elif geometry.geom_type == "MultiPolygon":
        geometry = MultiPolygon([orient(part) for part in geometry.geoms])
    return abs(GEOD.geometry_area_perimeter(geometry)[0])


def geodesic_length(geometry) -> float:
    return GEOD.geometry_length(geometry)


def reproject(geometry, source: int, target: int):
    transformer = Transformer.from_crs(source, target, always_xy=True)
    return shapely.transform(geometry, lambda c: np.column_stack(transformer.transform(c[:, 0], c[:, 1])))


class TestGeographicInput:
    def test_polygon_area_in_square_metres(self):
        result = measure_feature(BANGALORE_SQUARE, "EPSG:4326")

        assert result.status is MeasurementStatus.MEASURED
        assert result.measurement_type is MeasurementType.AREA
        assert result.unit == "m²"
        assert result.projected_crs == "EPSG:32643"  # UTM 43N
        assert result.value == pytest.approx(geodesic_area(BANGALORE_SQUARE), rel=AREA_TOLERANCE)
        assert result.value == pytest.approx(1.2e6, rel=0.01)  # ~1.1 km x 1.1 km
        assert result.value != pytest.approx(BANGALORE_SQUARE.area)  # not 0.0001 "square degrees"

    def test_linestring_length_in_metres(self):
        result = measure_feature(ROAD, "EPSG:4326")

        assert result.status is MeasurementStatus.MEASURED
        assert result.measurement_type is MeasurementType.LENGTH
        assert result.unit == "m"
        assert result.projected_crs == "EPSG:32643"
        assert result.value == pytest.approx(geodesic_length(ROAD), rel=LENGTH_TOLERANCE)

    def test_multipolygon_area_is_sum_of_parts(self):
        parts = [box(77.59, 12.97, 77.595, 12.975), box(77.60, 12.97, 77.605, 12.975)]

        result = measure_feature(MultiPolygon(parts), "EPSG:4326")

        assert result.status is MeasurementStatus.MEASURED
        assert result.value == pytest.approx(sum(map(geodesic_area, parts)), rel=AREA_TOLERANCE)

    def test_polygon_hole_is_excluded_from_area(self):
        with_hole = Polygon(BANGALORE_SQUARE.exterior.coords, [box(77.593, 12.973, 77.597, 12.977).exterior.coords])

        result = measure_feature(with_hole, "EPSG:4326")

        assert result.value == pytest.approx(geodesic_area(with_hole), rel=AREA_TOLERANCE)
        assert result.value < measure_feature(BANGALORE_SQUARE, "EPSG:4326").value

    def test_multilinestring_length_is_sum_of_parts(self):
        parts = [ROAD, LineString([(77.62, 12.97), (77.62, 12.98)])]

        result = measure_feature(MultiLineString(parts), "EPSG:4326")

        assert result.status is MeasurementStatus.MEASURED
        assert result.value == pytest.approx(sum(map(geodesic_length, parts)), rel=LENGTH_TOLERANCE)

    def test_linear_ring_is_measured_as_length(self):
        ring = LinearRing(BANGALORE_SQUARE.exterior.coords)

        result = measure_feature(ring, "EPSG:4326")

        assert result.measurement_type is MeasurementType.LENGTH
        assert result.value == pytest.approx(geodesic_length(ring), rel=LENGTH_TOLERANCE)

    def test_southern_hemisphere_uses_south_utm_zone(self):
        sydney = box(151.20, -33.87, 151.21, -33.86)

        result = measure_feature(sydney, "EPSG:4326")

        assert result.projected_crs == "EPSG:32756"
        assert result.value == pytest.approx(geodesic_area(sydney), rel=AREA_TOLERANCE)

    def test_other_geographic_datum(self):
        # NAD83 lon/lat (EPSG:4269), around San Francisco.
        sf = box(-122.42, 37.77, -122.41, 37.78)

        result = measure_feature(sf, "EPSG:4269")

        assert result.projected_crs == "EPSG:32610"
        assert result.value == pytest.approx(geodesic_area(sf), rel=AREA_TOLERANCE)

    def test_z_coordinates_are_ignored(self):
        polygon_3d = Polygon([(x, y, 900.0) for x, y in BANGALORE_SQUARE.exterior.coords])

        assert measure_feature(polygon_3d, "EPSG:4326").value == pytest.approx(
            measure_feature(BANGALORE_SQUARE, "EPSG:4326").value
        )


class TestProjectedInput:
    def test_metre_based_crs_is_used_directly(self):
        square = box(500_000, 1_400_000, 501_000, 1_401_000)  # 1 km x 1 km in UTM 43N

        result = measure_feature(square, "EPSG:32643")

        assert result.projected_crs == "EPSG:32643"
        assert result.value == pytest.approx(1_000_000.0)

    def test_line_in_metre_based_crs(self):
        result = measure_feature(LineString([(500_000, 1_400_000), (503_000, 1_404_000)]), "EPSG:32643")

        assert result.value == pytest.approx(5_000.0)

    def test_foot_based_crs_is_converted_to_metres(self):
        us_survey_foot = 1200 / 3937  # metres
        square = box(6_000_000, 2_100_000, 6_001_000, 2_101_000)  # 1000 ft square, CA State Plane III

        area = measure_feature(square, "EPSG:2227")
        length = measure_feature(LineString([(6_000_000, 2_100_000), (6_001_000, 2_100_000)]), "EPSG:2227")

        assert area.projected_crs == "EPSG:2227"
        assert area.value == pytest.approx(1_000_000 * us_survey_foot**2)
        assert length.value == pytest.approx(1000 * us_survey_foot)

    def test_web_mercator_is_reprojected_to_utm(self):
        # At 60°N Web Mercator inflates area ~4x; measuring in it directly would be badly wrong.
        lonlat = box(10.0, 60.0, 10.05, 60.05)
        mercator = reproject(lonlat, 4326, 3857)
        assert mercator.area / geodesic_area(lonlat) > 3.9

        result = measure_feature(mercator, "EPSG:3857")

        assert result.projected_crs == "EPSG:32632"
        assert result.value == pytest.approx(geodesic_area(lonlat), rel=AREA_TOLERANCE)


class TestNoMeasurement:
    @pytest.mark.parametrize("geometry", [Point(77.59, 12.97), MultiPoint([(77.59, 12.97), (77.6, 12.98)])])
    def test_points_are_not_applicable(self, geometry):
        result = measure_feature(geometry, "EPSG:4326")

        assert result.measurement_type is MeasurementType.NONE
        assert result.status is MeasurementStatus.NOT_APPLICABLE
        assert result.value is None
        assert "Point" in result.note

    def test_missing_crs_is_not_assumed(self):
        result = measure_feature(BANGALORE_SQUARE, None)

        assert result.measurement_type is MeasurementType.AREA
        assert result.status is MeasurementStatus.UNAVAILABLE
        assert result.value is None and result.projected_crs is None
        assert "CRS is unknown" in result.note

    def test_unsupported_geometry_type(self):
        collection = GeometryCollection([BANGALORE_SQUARE, ROAD])

        result = measure_feature(collection, "EPSG:4326")

        assert result.status is MeasurementStatus.UNAVAILABLE
        assert result.measurement_type is MeasurementType.NONE
        assert "GeometryCollection" in result.note

    @pytest.mark.parametrize(
        ("geometry", "kind"), [(Polygon(), MeasurementType.AREA), (LineString(), MeasurementType.LENGTH)]
    )
    def test_empty_geometry(self, geometry, kind):
        result = measure_feature(geometry, "EPSG:4326")

        assert result.status is MeasurementStatus.UNAVAILABLE
        assert result.measurement_type is kind
        assert result.note == "Geometry is empty."

    def test_missing_geometry(self):
        result = measure_feature(None, "EPSG:4326")

        assert result.status is MeasurementStatus.UNAVAILABLE
        assert result.note == "Feature has no geometry."

    def test_polar_feature_is_outside_utm(self):
        result = measure_feature(box(10.0, 85.0, 10.1, 85.1), "EPSG:4326")

        assert result.status is MeasurementStatus.UNAVAILABLE
        assert "UTM coverage" in result.note

    def test_feature_wider_than_a_utm_zone(self):
        result = measure_feature(LineString([(70.0, 12.0), (80.0, 12.0)]), "EPSG:4326")

        assert result.status is MeasurementStatus.UNAVAILABLE
        assert "wider than one UTM zone" in result.note

    def test_mislabelled_crs_is_detected(self):
        # Metre coordinates in a file that claims to be lon/lat.
        result = measure_feature(box(500_000, 1_400_000, 501_000, 1_401_000), "EPSG:4326")

        assert result.status is MeasurementStatus.UNAVAILABLE
        assert "mislabelled" in result.note

    def test_uninterpretable_crs(self):
        result = measure_feature(BANGALORE_SQUARE, "NOT A CRS")

        assert result.status is MeasurementStatus.UNAVAILABLE
        assert "could not be interpreted" in result.note


class TestInvalidGeometry:
    def test_self_intersecting_polygon_is_repaired_and_flagged(self):
        # 'Bowtie': as drawn, its two lobes cancel out and shapely reports area 0.
        bowtie = Polygon([(77.59, 12.97), (77.60, 12.98), (77.60, 12.97), (77.59, 12.98)])
        assert not bowtie.is_valid and bowtie.area == 0
        expected = geodesic_area(shapely.make_valid(bowtie, method="structure"))

        result = measure_feature(bowtie, "EPSG:4326")

        assert result.status is MeasurementStatus.MEASURED
        assert result.value == pytest.approx(expected, rel=AREA_TOLERANCE)
        assert result.value > 0
        assert "invalid (Self-intersection" in result.note
        assert "repair" in result.note

    def test_valid_geometry_has_no_note(self):
        assert measure_feature(BANGALORE_SQUARE, "EPSG:4326").note is None

    def test_polygon_that_collapses_on_repair(self):
        sliver = Polygon([(77.59, 12.97), (77.60, 12.97), (77.61, 12.97)])  # zero-area, all points collinear

        result = measure_feature(sliver, "EPSG:4326")

        assert result.status is MeasurementStatus.UNAVAILABLE
        assert "invalid" in result.note


class TestSummary:
    M, NA, U = MeasurementStatus.MEASURED, MeasurementStatus.NOT_APPLICABLE, MeasurementStatus.UNAVAILABLE
    AREA, LENGTH, NONE = MeasurementType.AREA, MeasurementType.LENGTH, MeasurementType.NONE

    def test_complete_totals(self):
        summary = summarize(
            [
                (self.AREA, self.M, 100.0),
                (self.AREA, self.M, 50.0),
                (self.LENGTH, self.M, 7.0),
                (self.NONE, self.NA, None),
            ]
        )

        assert summary.total_area_m2 == 150.0
        assert summary.total_length_m == 7.0
        assert (summary.measured_count, summary.not_applicable_count, summary.unavailable_count) == (3, 1, 0)
        assert summary.totals_complete is True
        assert summary.note is None

    def test_unmeasured_polygon_nulls_area_total_only(self):
        summary = summarize([(self.AREA, self.M, 100.0), (self.AREA, self.U, None), (self.LENGTH, self.M, 7.0)])

        assert summary.total_area_m2 is None
        assert summary.total_length_m == 7.0
        assert summary.totals_complete is False
        assert "1 feature(s) could not be measured" in summary.note

    def test_unmeasured_unsupported_feature_nulls_both_totals(self):
        summary = summarize([(self.AREA, self.M, 100.0), (self.LENGTH, self.M, 7.0), (self.NONE, self.U, None)])

        assert summary.total_area_m2 is None
        assert summary.total_length_m is None

    def test_points_only_gives_zero_totals(self):
        summary = summarize([(self.NONE, self.NA, None)])

        assert summary.total_area_m2 == 0
        assert summary.total_length_m == 0
        assert summary.totals_complete is True
