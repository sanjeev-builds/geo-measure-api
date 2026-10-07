import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.models import FileType
from app.services.reader import KML_DISPLAY_FIELDS, ReaderError, read_features, to_json_value
from tests.factories import SAMPLE_KML, line_frame, sample_frame, write_shapefile


def read_shapefile(tmp_path, frame):
    write_shapefile(tmp_path, frame)
    return read_features(tmp_path / "parcels.shp", FileType.SHAPEFILE)


def read_kml(tmp_path, content: bytes = SAMPLE_KML):
    path = tmp_path / "survey.kml"
    path.write_bytes(content)
    return read_features(path, FileType.KML)


class TestShapefile:
    def test_extracts_every_feature_with_index_type_and_geometry(self, tmp_path):
        result = read_shapefile(tmp_path, sample_frame())

        assert [f.index for f in result.features] == [0, 1, 2]
        assert [f.geometry_type for f in result.features] == ["Polygon", "Polygon", "MultiPolygon"]
        assert result.features[0].geometry.equals(sample_frame().geometry[0])

    def test_reads_line_shapefile(self, tmp_path):
        result = read_shapefile(tmp_path, line_frame())

        assert [f.geometry_type for f in result.features] == ["LineString"]
        assert result.features[0].properties == {"name": "Access road"}

    def test_extracts_properties_and_omits_nulls(self, tmp_path):
        result = read_shapefile(tmp_path, sample_frame())

        assert result.features[0].properties == {"name": "Plot A", "owner": "Ravi", "survey_no": 42}
        assert result.features[1].properties == {"name": "Plot B", "survey_no": 7}

    def test_reports_declared_geographic_crs(self, tmp_path):
        result = read_shapefile(tmp_path, sample_frame("EPSG:4326"))

        assert result.crs == "EPSG:4326"
        assert {f.crs for f in result.features} == {"EPSG:4326"}

    def test_reports_declared_projected_crs(self, tmp_path):
        frame = sample_frame().to_crs("EPSG:32643")  # UTM 43N, covers Bangalore

        result = read_shapefile(tmp_path, frame)

        assert result.crs == "EPSG:32643"

    def test_missing_prj_means_no_crs_is_assumed(self, tmp_path):
        write_shapefile(tmp_path, sample_frame(crs=None))
        assert not (tmp_path / "parcels.prj").exists()

        result = read_features(tmp_path / "parcels.shp", FileType.SHAPEFILE)

        assert result.crs is None
        assert all(f.crs is None for f in result.features)
        assert len(result.features) == 3

    def test_corrupt_shp_raises_reader_error(self, tmp_path):
        write_shapefile(tmp_path, sample_frame())
        (tmp_path / "parcels.shp").write_bytes(b"garbage, not a shapefile header")

        with pytest.raises(ReaderError, match="parcels.shp"):
            read_features(tmp_path / "parcels.shp", FileType.SHAPEFILE)


class TestKml:
    def test_extracts_features_from_all_folders(self, tmp_path):
        result = read_kml(tmp_path)

        assert [f.index for f in result.features] == [0, 1, 2]
        assert sorted(f.geometry_type for f in result.features) == ["LineString", "Point", "Polygon"]

    def test_crs_comes_from_driver_not_invented(self, tmp_path):
        result = read_kml(tmp_path)

        # GDAL's KML driver declares WGS84 because the KML 2.2 spec mandates it.
        assert result.crs == "EPSG:4326"
        assert {f.crs for f in result.features} == {"EPSG:4326"}

    def test_extracts_name_description_and_extended_data(self, tmp_path):
        result = read_kml(tmp_path)
        plot = next(f for f in result.features if f.geometry_type == "Polygon")

        assert plot.properties["Name"] == "Plot A"
        assert plot.properties["description"] == "Farm plot"
        assert plot.properties["owner"] == "Ravi"
        assert plot.properties["survey_no"] == "42"  # KML <Data> values are untyped strings

    def test_drops_rendering_fields(self, tmp_path):
        result = read_kml(tmp_path)

        for feature in result.features:
            assert not KML_DISPLAY_FIELDS & feature.properties.keys()

    def test_malformed_kml_raises_reader_error(self, tmp_path):
        with pytest.raises(ReaderError):
            read_kml(tmp_path, b"<kml><Placemark><unclosed")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (math.nan, None),
        (pd.NaT, None),
        (np.int64(5), 5),
        (np.float64(2.5), 2.5),
        (np.bool_(True), True),
        (pd.Timestamp("2024-01-02T03:04:05"), "2024-01-02T03:04:05"),
        (date(2024, 1, 2), "2024-01-02"),
        (b"bytes", "bytes"),
        ("text", "text"),
    ],
)
def test_to_json_value(raw, expected):
    assert to_json_value(raw) == expected
