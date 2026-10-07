"""Background job: turn a stored upload into measured Feature rows.

Status flow: PENDING -> PROCESSING -> COMPLETED, or FAILED with a stored error.
Runs via FastAPI BackgroundTasks, i.e. in-process; it is not a durable queue.
"""

import logging
import shutil
from pathlib import Path

from shapely.geometry import mapping
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Feature, FileStatus, FileType, UploadedFile, utcnow
from app.services.ingest import IngestError, extract_zip, locate_shapefile, stored_upload_path
from app.services.measure import measure_feature
from app.services.reader import ExtractedFeature, ReaderError, read_features

logger = logging.getLogger(__name__)


def process_file(file_id: str, session_factory: sessionmaker[Session], settings: Settings) -> None:
    with session_factory() as session:
        record = session.get(UploadedFile, file_id)
        if record is None:
            logger.warning("process_file: no upload with id %s", file_id)
            return

        record.status = FileStatus.PROCESSING
        record.error = None
        session.commit()

        try:
            source = resolve_source_path(record, settings)
            result = read_features(source, record.file_type)

            # Replace rather than append, so re-processing a file is idempotent.
            session.execute(delete(Feature).where(Feature.file_id == record.id))
            # A feature that cannot be measured is recorded as such; it never fails the whole file.
            session.add_all(build_feature_row(record.id, feature) for feature in result.features)
            record.crs = result.crs
            record.feature_count = len(result.features)
            record.status = FileStatus.COMPLETED
            record.processed_at = utcnow()
            session.commit()
        except (IngestError, ReaderError) as exc:
            _mark_failed(session, record, str(exc))
        except Exception as exc:  # noqa: BLE001 - a background job must never die silently
            logger.exception("Unexpected error processing file %s", file_id)
            _mark_failed(session, record, f"Unexpected error while processing file ({exc.__class__.__name__}).")
        finally:
            # The extracted copy is only needed while GDAL reads it; the original upload is kept.
            shutil.rmtree(extraction_dir(settings, file_id), ignore_errors=True)


def build_feature_row(file_id: str, feature: ExtractedFeature) -> Feature:
    measurement = measure_feature(feature.geometry, feature.crs)
    return Feature(
        file_id=file_id,
        index=feature.index,
        geometry_type=feature.geometry_type,
        geometry=mapping(feature.geometry) if feature.geometry is not None else None,
        properties=feature.properties,
        crs=feature.crs,
        measurement_type=measurement.measurement_type,
        measurement_status=measurement.status,
        value=measurement.value,
        unit=measurement.unit,
        projected_crs=measurement.projected_crs,
        note=measurement.note,
    )


def resolve_source_path(record: UploadedFile, settings: Settings) -> Path:
    """Return the path GDAL should open: the .kml itself, or the .shp inside the extracted zip."""
    stored = stored_upload_path(settings.upload_dir / record.id, record.file_type)
    if record.file_type is FileType.KML:
        return stored

    extracted = extract_zip(
        stored,
        extraction_dir(settings, record.id),
        max_total_bytes=settings.max_extracted_bytes,
        max_members=settings.max_archive_members,
    )
    return locate_shapefile(extracted)


def extraction_dir(settings: Settings, file_id: str) -> Path:
    return settings.upload_dir / file_id / "extracted"


def _mark_failed(session: Session, record: UploadedFile, message: str) -> None:
    session.rollback()
    record.status = FileStatus.FAILED
    record.error = message
    record.feature_count = None
    record.processed_at = utcnow()
    session.commit()
