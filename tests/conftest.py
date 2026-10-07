from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

MINIMAL_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Placemark><name>p</name><Point><coordinates>77.59,12.97</coordinates></Point></Placemark>
</kml>
"""


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        upload_dir=tmp_path / "uploads",
        max_upload_bytes=64 * 1024,
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    # Context manager runs the lifespan (creates tables and the upload dir).
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_session(app):
    session = app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
