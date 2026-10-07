"""GET /api/files/{id}/ and GET /api/files/{id}/measurements/."""

import uuid

import pytest
from pyproj import Geod

from app.models import FileStatus, FileType, UploadedFile
from tests.factories import BANGALORE_SQUARE, SAMPLE_KML, sample_frame, shapefile_zip

GEOD = Geod(ellps="WGS84")


def upload(client, filename: str, content: bytes) -> str:
    response = client.post("/api/files/", files={"file": (filename, content, "application/octet-stream")})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def insert_record(app, status: FileStatus, error: str | None = None) -> str:
    file_id = uuid.uuid4().hex
    with app.state.session_factory() as session:
        session.add(UploadedFile(id=file_id, filename="x.kml", file_type=FileType.KML, status=status, error=error))
        session.commit()
    return file_id


class TestGetFile:
    def test_returns_file_information(self, client):
        file_id = upload(client, "survey.kml", SAMPLE_KML)

        response = client.get(f"/api/files/{file_id}/")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == file_id
        assert body["filename"] == "survey.kml"
        assert body["feature_count"] == 3
        assert body["crs"] == "EPSG:4326"
        assert body["status"] == "COMPLETED"
        assert body["error"] is None
        assert body["created_at"].endswith("Z") and body["processed_at"].endswith("Z")  # UTC, tz-aware

    def test_failed_file_exposes_error(self, client):
        file_id = upload(client, "parcels.zip", b"not a zip")

        body = client.get(f"/api/files/{file_id}/").json()

        assert body["status"] == "FAILED"
        assert "not a valid zip" in body["error"]

    def test_unknown_id_returns_404(self, client):
        response = client.get("/api/files/doesnotexist/")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]


class TestGetMeasurements:
    def test_kml_measurements(self, client):
        file_id = upload(client, "survey.kml", SAMPLE_KML)

        response = client.get(f"/api/files/{file_id}/measurements/")

        assert response.status_code == 200
        body = response.json()
        assert body["file_id"] == file_id
        assert body["crs"] == "EPSG:4326"
        rows = {m["geometry_type"]: m for m in body["measurements"]}
        assert [m["feature_index"] for m in body["measurements"]] == [0, 1, 2]

        polygon = rows["Polygon"]
        assert polygon["measurement_type"] == "AREA"
        assert polygon["measurement_status"] == "MEASURED"
        assert polygon["unit"] == "m²"
        assert polygon["projected_crs"] == "EPSG:32643"
        assert polygon["value"] == pytest.approx(abs(GEOD.geometry_area_perimeter(BANGALORE_SQUARE)[0]), rel=0.005)

        assert rows["LineString"]["measurement_type"] == "LENGTH"
        assert rows["LineString"]["unit"] == "m"
        assert rows["LineString"]["value"] > 0

        assert rows["Point"]["measurement_status"] == "NOT_APPLICABLE"
        assert rows["Point"]["value"] is None

        summary = body["summary"]
        assert summary["feature_count"] == 3
        assert summary["measured_count"] == 2
        assert summary["not_applicable_count"] == 1
        assert summary["unavailable_count"] == 0
        assert summary["totals_complete"] is True
        assert summary["total_area_m2"] == pytest.approx(polygon["value"])
        assert summary["total_length_m"] == pytest.approx(rows["LineString"]["value"])

    def test_shapefile_measurements(self, client, tmp_path):
        file_id = upload(client, "parcels.zip", shapefile_zip(tmp_path / "shp"))

        body = client.get(f"/api/files/{file_id}/measurements/").json()

        values = [m["value"] for m in body["measurements"]]
        assert all(m["measurement_status"] == "MEASURED" for m in body["measurements"])
        assert body["summary"]["total_area_m2"] == pytest.approx(sum(values))
        assert body["summary"]["total_length_m"] == 0

    def test_projected_shapefile_measurements_match_geographic(self, client, tmp_path):
        geographic = upload(client, "a.zip", shapefile_zip(tmp_path / "a"))
        projected = upload(client, "b.zip", shapefile_zip(tmp_path / "b", sample_frame().to_crs("EPSG:32643")))

        geo_total = client.get(f"/api/files/{geographic}/measurements/").json()["summary"]["total_area_m2"]
        utm_body = client.get(f"/api/files/{projected}/measurements/").json()

        assert {m["projected_crs"] for m in utm_body["measurements"]} == {"EPSG:32643"}
        assert utm_body["summary"]["total_area_m2"] == pytest.approx(geo_total, rel=1e-6)

    def test_missing_crs_reports_unavailable_without_misleading_totals(self, client, tmp_path):
        file_id = upload(client, "nocrs.zip", shapefile_zip(tmp_path / "shp", sample_frame(crs=None)))

        response = client.get(f"/api/files/{file_id}/measurements/")

        assert response.status_code == 200
        body = response.json()
        assert body["crs"] is None
        assert all(m["measurement_status"] == "UNAVAILABLE" for m in body["measurements"])
        assert all("CRS is unknown" in m["note"] for m in body["measurements"])
        assert body["summary"]["total_area_m2"] is None
        assert body["summary"]["totals_complete"] is False
        assert body["summary"]["unavailable_count"] == 3

    @pytest.mark.parametrize("status", [FileStatus.PENDING, FileStatus.PROCESSING])
    def test_processing_not_complete_returns_409(self, client, app, status):
        file_id = insert_record(app, status)

        response = client.get(f"/api/files/{file_id}/measurements/")

        assert response.status_code == 409
        assert status.value in response.json()["detail"]

    def test_failed_processing_returns_422_with_error(self, client):
        file_id = upload(client, "parcels.zip", b"not a zip")

        response = client.get(f"/api/files/{file_id}/measurements/")

        assert response.status_code == 422
        assert response.json()["detail"].startswith("Processing failed")
        assert "not a valid zip" in response.json()["detail"]

    def test_unknown_id_returns_404(self, client):
        response = client.get("/api/files/doesnotexist/measurements/")

        assert response.status_code == 404
