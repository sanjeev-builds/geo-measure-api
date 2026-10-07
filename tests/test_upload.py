import io
import zipfile

import pytest

from app.models import FileStatus, FileType, UploadedFile
from tests.conftest import MINIMAL_KML


def upload(client, filename: str, content: bytes):
    return client.post("/api/files/", files={"file": (filename, content, "application/octet-stream")})


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_upload_kml_is_accepted_and_stored(client, settings, db_session):
    response = upload(client, "survey.kml", MINIMAL_KML)

    assert response.status_code == 202
    body = response.json()
    assert body["filename"] == "survey.kml"
    assert body["file_type"] == "KML"
    assert body["status"] == "PENDING"
    assert body["feature_count"] is None

    stored = [p for p in (settings.upload_dir / body["id"]).iterdir() if p.is_file()]
    assert [p.read_bytes() for p in stored] == [MINIMAL_KML]

    # The response is sent before processing; TestClient then runs the background task.
    record = db_session.get(UploadedFile, body["id"])
    assert record is not None
    assert record.file_type is FileType.KML
    assert record.status is FileStatus.COMPLETED


def test_upload_zip_is_classified_as_shapefile(client):
    response = upload(client, "parcels.zip", make_zip({"parcels.shp": b"x"}))

    assert response.status_code == 202
    assert response.json()["file_type"] == "SHAPEFILE"


def test_extension_check_is_case_insensitive(client):
    response = upload(client, "SURVEY.KML", MINIMAL_KML)

    assert response.status_code == 202
    assert response.json()["file_type"] == "KML"


@pytest.mark.parametrize("filename", ["notes.txt", "parcels.shp", "data.geojson", "noextension"])
def test_unsupported_extension_is_rejected(client, settings, filename):
    response = upload(client, filename, b"data")

    assert response.status_code == 400
    assert "Allowed extensions" in response.json()["detail"]
    assert not any(settings.upload_dir.iterdir())


def test_empty_file_is_rejected_and_not_persisted(client, settings, db_session):
    response = upload(client, "empty.kml", b"")

    assert response.status_code == 400
    assert not any(settings.upload_dir.iterdir())
    assert db_session.query(UploadedFile).count() == 0


def test_oversized_file_is_rejected_and_cleaned_up(client, settings, db_session):
    response = upload(client, "big.kml", b"x" * (settings.max_upload_bytes + 1))

    assert response.status_code == 413
    assert not any(settings.upload_dir.iterdir())
    assert db_session.query(UploadedFile).count() == 0


@pytest.mark.parametrize("filename", ["../../evil.kml", "..\\..\\evil.kml", "/etc/evil.kml"])
def test_path_components_in_filename_are_stripped(client, settings, tmp_path, filename):
    response = upload(client, filename, MINIMAL_KML)

    assert response.status_code == 202
    assert response.json()["filename"] == "evil.kml"
    assert not list(tmp_path.rglob("evil.kml"))  # the client's name never becomes a path on disk
    assert [p.name for p in settings.upload_dir.iterdir()] == [response.json()["id"]]


@pytest.mark.parametrize(
    "filename",
    [
        "evil:stream.kml",  # NTFS alternate data stream
        "a<b>|?.kml",  # characters invalid on Windows
        "CON.kml",  # Windows reserved device name
        "x" * 300 + ".kml",  # longer than filesystem/DB limits
        "tab\tand\x00null.kml",  # control characters
    ],
)
def test_hostile_filenames_are_accepted_safely(client, filename):
    response = upload(client, filename, MINIMAL_KML)

    assert response.status_code == 202
    body = response.json()
    assert len(body["filename"]) <= 255
    assert body["filename"].endswith(".kml")
    assert not any(ord(ch) < 32 for ch in body["filename"])
    assert client.get(f"/api/files/{body['id']}/").json()["status"] == "COMPLETED"


def test_oversized_request_is_rejected_before_the_body_is_read(client, settings, db_session):
    # Exceeds the Content-Length limit (upload limit + multipart allowance), so the middleware answers.
    response = upload(client, "huge.kml", b"x" * (settings.max_upload_bytes + 128 * 1024))

    assert response.status_code == 413
    assert "Request body exceeds" in response.json()["detail"]
    assert not any(settings.upload_dir.iterdir())
    assert db_session.query(UploadedFile).count() == 0


def test_missing_file_field_returns_422(client):
    response = client.post("/api/files/")

    assert response.status_code == 422
