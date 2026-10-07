import pytest
from pyproj import CRS
from shapely.geometry import Point, box

from app.services.crs import CrsSelectionError, select_measurement_crs

WGS84 = CRS.from_epsg(4326)


@pytest.mark.parametrize(
    ("lon", "lat", "expected"),
    [
        (77.59, 12.97, "EPSG:32643"),  # Bangalore, UTM 43N
        (72.88, 19.08, "EPSG:32643"),  # Mumbai, UTM 43N
        (88.36, 22.57, "EPSG:32645"),  # Kolkata, UTM 45N
        (-0.13, 51.51, "EPSG:32630"),  # London, UTM 30N
        (151.21, -33.87, "EPSG:32756"),  # Sydney, UTM 56S
        (-74.01, 40.71, "EPSG:32618"),  # New York, UTM 18N
    ],
)
def test_geographic_input_selects_utm_zone_from_location(lon, lat, expected):
    choice = select_measurement_crs(Point(lon, lat).buffer(0.001), WGS84)

    assert choice.label == expected
    assert choice.metres_per_unit == 1.0


def test_zone_is_chosen_from_feature_centre():
    # Spans the 78°E boundary between zones 43 and 44, but is centred in zone 44.
    choice = select_measurement_crs(box(77.9, 12.9, 78.5, 13.0), WGS84)

    assert choice.label == "EPSG:32644"


@pytest.mark.parametrize(("lon", "lat"), [(78.0, 12.9), (77.6, 0.0)])  # zone boundary, equator
def test_ambiguous_locations_are_resolved_deterministically(lon, lat):
    first = select_measurement_crs(Point(lon, lat), WGS84)
    second = select_measurement_crs(Point(lon, lat), WGS84)

    assert first.label == second.label
    assert first.crs.is_projected


def test_projected_crs_is_kept():
    choice = select_measurement_crs(box(500_000, 1_400_000, 501_000, 1_401_000), CRS.from_epsg(32643))

    assert choice.label == "EPSG:32643"
    assert choice.metres_per_unit == 1.0


def test_projected_crs_in_feet_reports_conversion_factor():
    choice = select_measurement_crs(box(6_000_000, 2_100_000, 6_001_000, 2_101_000), CRS.from_epsg(2227))

    assert choice.metres_per_unit == pytest.approx(1200 / 3937)


@pytest.mark.parametrize(
    ("world_crs", "bangalore_xy"),
    [
        ("EPSG:3857", (8_637_000, 1_456_000)),  # Web Mercator
        ("EPSG:3395", (8_637_000, 1_447_000)),  # World Mercator
        ("EPSG:4087", (8_637_000, 1_443_000)),  # World Equidistant Cylindrical (Plate Carrée)
    ],
)
def test_world_cylindrical_projections_are_not_trusted_for_measurement(world_crs, bangalore_xy):
    x, y = bangalore_xy
    choice = select_measurement_crs(box(x, y, x + 1000, y + 1000), CRS.from_user_input(world_crs))

    assert choice.label == "EPSG:32643"


def test_missing_crs_raises():
    with pytest.raises(CrsSelectionError, match="not assumed"):
        select_measurement_crs(box(77.59, 12.97, 77.6, 12.98), None)


def test_geocentric_crs_is_unsupported():
    with pytest.raises(CrsSelectionError, match="Unsupported source CRS type"):
        select_measurement_crs(Point(0, 0), CRS.from_epsg(4978))
