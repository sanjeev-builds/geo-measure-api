"""End-to-end processing through the upload endpoint, plus direct processor tests."""

import uuid

import pytest
from sqlalchemy import select

from app.models import Feature, FileStatus, FileType, UploadedFile
from app.services import processor
from app.services.ingest import stored_upload_path
from app.services.processor import process_file
from tests.factories import SAMPLE_KML, make_zip, sample_frame, shapefile_zip


@pytest.fixture
def upload_and_fetch(client, app):
    """Upload a file and return (response_json, record, features) after background processing."""

    def _upload(filename: str, content: bytes):
        response = client.post("/api/files/", files={"file": (filename, content, "application/octet-stream")})
        assert response.status_code == 202, response.text
        body = response.json()
        with app.state.session_factory() as session:
            record = session.get(UploadedFile, body["id"])
            features = session.scalars(
                select(Feature).where(Feature.file_id == body["id"]).order_by(Feature.index)
            ).all()
        return body, record, features

    return _upload


class TestSuccessfulProcessing:
    def test_valid_kml(self, upload_and_fetch):
        body, record, features = upload_and_fetch("survey.kml", SAMPLE_KML)

        assert body["status"] == "PENDING"  # response is sent before processing runs
        assert record.status is FileStatus.COMPLETED
        assert record.error is None
        assert record.feature_count == 3
        assert record.crs == "EPSG:4326"
        assert record.processed_at is not None
        assert [f.index for f in features] == [0, 1, 2]

    def test_valid_shapefile_zip(self, upload_and_fetch, tmp_path):
        _, record, features = upload_and_fetch("parcels.zip", shapefile_zip(tmp_path / "shp"))

        assert record.status is FileStatus.COMPLETED
        assert record.feature_count == 3
        assert record.crs == "EPSG:4326"
        assert [f.geometry_type for f in features] == ["Polygon", "Polygon", "MultiPolygon"]

    def test_shapefile_inside_a_folder_in_the_zip(self, upload_and_fetch, tmp_path):
        _, record, _ = upload_and_fetch("parcels.zip", shapefile_zip(tmp_path / "shp", prefix="export/data/"))

        assert record.status is FileStatus.COMPLETED
        assert record.feature_count == 3

    def test_features_store_geojson_geometry_properties_and_crs(self, upload_and_fetch, tmp_path):
        _, _, features = upload_and_fetch("parcels.zip", shapefile_zip(tmp_path / "shp"))
        plot_a, plot_b, plot_c = features

        assert plot_a.geometry["type"] == "Polygon"
        assert plot_a.geometry["coordinates"][0][0] == pytest.approx((77.59, 12.97))
        assert plot_c.geometry["type"] == "MultiPolygon"
        assert len(plot_c.geometry["coordinates"]) == 2
        assert plot_a.properties == {"name": "Plot A", "owner": "Ravi", "survey_no": 42}
        assert plot_b.properties == {"name": "Plot B", "survey_no": 7}
        assert {f.crs for f in features} == {"EPSG:4326"}

    def test_projected_shapefile_keeps_its_crs(self, upload_and_fetch, tmp_path):
        zipped = shapefile_zip(tmp_path / "shp", sample_frame().to_crs("EPSG:32643"))

        _, record, features = upload_and_fetch("utm.zip", zipped)

        assert record.crs == "EPSG:32643"
        assert {f.crs for f in features} == {"EPSG:32643"}

    def test_missing_prj_completes_without_assuming_a_crs(self, upload_and_fetch, tmp_path):
        zipped = shapefile_zip(tmp_path / "shp", sample_frame(crs=None))

        _, record, features = upload_and_fetch("nocrs.zip", zipped)

        assert record.status is FileStatus.COMPLETED
        assert record.crs is None
        assert record.feature_count == 3
        assert all(f.crs is None for f in features)

    def test_features_are_measured_during_processing(self, upload_and_fetch):
        _, _, features = upload_and_fetch("survey.kml", SAMPLE_KML)
        by_type = {f.geometry_type: f for f in features}

        assert by_type["Polygon"].unit == "m²" and by_type["Polygon"].projected_crs == "EPSG:32643"
        assert by_type["LineString"].unit == "m"
        assert by_type["Point"].value is None


class TestFailedProcessing:
    @pytest.mark.parametrize(
        ("excluded", "expected_error"),
        [
            ((".shp",), "No .shp file found"),
            ((".shx",), "missing required companion file(s): parcels.shx"),
            ((".dbf",), "missing required companion file(s): parcels.dbf"),
            ((".shx", ".dbf"), "parcels.shx, parcels.dbf"),
        ],
    )
    def test_incomplete_shapefile_archive(self, upload_and_fetch, tmp_path, excluded, expected_error):
        _, record, features = upload_and_fetch("parcels.zip", shapefile_zip(tmp_path / "shp", exclude=excluded))

        assert record.status is FileStatus.FAILED
        assert expected_error in record.error
        assert record.feature_count is None
        assert features == []

    def test_malformed_zip(self, upload_and_fetch):
        _, record, _ = upload_and_fetch("parcels.zip", b"PK\x03\x04 this is not really a zip")

        assert record.status is FileStatus.FAILED
        assert "corrupt or not a valid zip" in record.error

    def test_server_paths_are_not_leaked_in_errors(self, upload_and_fetch, settings):
        zipped = make_zip({"parcels.shp": b"junk", "parcels.shx": b"junk", "parcels.dbf": b"junk"})

        _, shp_record, _ = upload_and_fetch("parcels.zip", zipped)
        _, kml_record, _ = upload_and_fetch("broken.kml", b"<kml><Placemark><unclosed")

        for error in (shp_record.error, kml_record.error):
            assert str(settings.upload_dir.resolve()) not in error
            assert settings.upload_dir.resolve().as_posix() not in error
            assert ":\\" not in error and "/tmp" not in error

    def test_extracted_files_are_cleaned_up(self, upload_and_fetch, settings, tmp_path):
        for content in (shapefile_zip(tmp_path / "ok"), shapefile_zip(tmp_path / "bad", exclude=(".dbf",))):
            body, _, _ = upload_and_fetch("parcels.zip", content)

            remaining = sorted(p.name for p in (settings.upload_dir / body["id"]).iterdir())
            assert remaining == ["source.zip"]

    def test_zip_slip_archive(self, upload_and_fetch, settings):
        _, record, _ = upload_and_fetch("evil.zip", make_zip({"../../escaped.txt": b"x"}))

        assert record.status is FileStatus.FAILED
        assert "outside the archive" in record.error
        assert not (settings.upload_dir / "escaped.txt").exists()

    def test_corrupt_shapefile_contents(self, upload_and_fetch):
        zipped = make_zip({"parcels.shp": b"junk", "parcels.shx": b"junk", "parcels.dbf": b"junk"})

        _, record, _ = upload_and_fetch("parcels.zip", zipped)

        assert record.status is FileStatus.FAILED
        assert record.error.startswith("Could not read 'parcels.shp'")

    def test_malformed_kml(self, upload_and_fetch):
        _, record, _ = upload_and_fetch("broken.kml", b"<kml><Placemark><unclosed")

        assert record.status is FileStatus.FAILED
        assert "Could not read" in record.error

    def test_unexpected_error_is_caught_and_recorded(self, upload_and_fetch, monkeypatch):
        def explode(*_args, **_kwargs):
            raise RuntimeError("internal detail that should not leak")

        monkeypatch.setattr(processor, "read_features", explode)

        _, record, features = upload_and_fetch("survey.kml", SAMPLE_KML)

        assert record.status is FileStatus.FAILED
        assert record.error == "Unexpected error while processing file (RuntimeError)."
        assert features == []


class TestStatusTransitions:
    def _create_record(self, app, settings, content: bytes = SAMPLE_KML) -> str:
        file_id = uuid.uuid4().hex
        stored = stored_upload_path(settings.upload_dir / file_id, FileType.KML)
        stored.parent.mkdir(parents=True)
        stored.write_bytes(content)
        with app.state.session_factory() as session:
            session.add(UploadedFile(id=file_id, filename="survey.kml", file_type=FileType.KML))
            session.commit()
        return file_id

    def _status(self, app, file_id) -> FileStatus:
        with app.state.session_factory() as session:
            return session.get(UploadedFile, file_id).status

    def test_pending_then_processing_then_completed(self, client, app, settings, monkeypatch):
        file_id = self._create_record(app, settings)
        seen_while_reading = []
        real_read = processor.read_features

        def spy(*args, **kwargs):
            seen_while_reading.append(self._status(app, file_id))
            return real_read(*args, **kwargs)

        monkeypatch.setattr(processor, "read_features", spy)

        assert self._status(app, file_id) is FileStatus.PENDING
        process_file(file_id, app.state.session_factory, settings)

        assert seen_while_reading == [FileStatus.PROCESSING]
        assert self._status(app, file_id) is FileStatus.COMPLETED

    def test_processing_then_failed(self, client, app, settings):
        file_id = self._create_record(app, settings, content=b"not kml")

        process_file(file_id, app.state.session_factory, settings)

        assert self._status(app, file_id) is FileStatus.FAILED

    def test_reprocessing_replaces_features_instead_of_duplicating(self, client, app, settings):
        file_id = self._create_record(app, settings)

        process_file(file_id, app.state.session_factory, settings)
        process_file(file_id, app.state.session_factory, settings)

        with app.state.session_factory() as session:
            count = len(session.scalars(select(Feature).where(Feature.file_id == file_id)).all())
            record = session.get(UploadedFile, file_id)
        assert count == record.feature_count == 3

    def test_unknown_file_id_is_ignored(self, client, app, settings):
        process_file("does-not-exist", app.state.session_factory, settings)
