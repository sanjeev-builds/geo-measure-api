import shutil
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.models import FileStatus, UploadedFile
from app.schemas import ErrorOut, FeatureMeasurementOut, FileOut, MeasurementsOut, MeasurementSummaryOut
from app.services.ingest import (
    EmptyFileError,
    FileTooLargeError,
    UnsupportedFileTypeError,
    detect_file_type,
    safe_filename,
    save_upload,
    stored_upload_path,
)
from app.services.measure import summarize
from app.services.processor import process_file

router = APIRouter(prefix="/api/files", tags=["files"])


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post(
    "/",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=FileOut,
    responses={400: {"model": ErrorOut}, 413: {"model": ErrorOut}},
)
def upload_file(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="A .zip containing a Shapefile, or a .kml file"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UploadedFile:
    """Accept a geospatial file, store it, and queue it for processing.

    Returns 202 with status PENDING; poll GET /api/files/{id}/ for the result.
    A plain `def` endpoint: file writes and DB calls are blocking, so FastAPI runs it in its threadpool
    rather than on the event loop.
    """
    try:
        file_type = detect_file_type(file.filename)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    file_id = uuid.uuid4().hex
    filename = safe_filename(file.filename)
    file_dir = settings.upload_dir / file_id

    try:
        save_upload(file.file, stored_upload_path(file_dir, file_type), settings.max_upload_bytes)
    except FileTooLargeError as exc:
        shutil.rmtree(file_dir, ignore_errors=True)
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc
    except EmptyFileError as exc:
        shutil.rmtree(file_dir, ignore_errors=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    record = UploadedFile(id=file_id, filename=filename, file_type=file_type, status=FileStatus.PENDING)
    db.add(record)
    try:
        db.commit()
    except Exception:
        db.rollback()
        shutil.rmtree(file_dir, ignore_errors=True)
        raise

    background_tasks.add_task(process_file, file_id, request.app.state.session_factory, settings)
    return record


def get_file_or_404(db: Session, file_id: str) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"File '{file_id}' not found.")
    return record


@router.get("/{file_id}/", response_model=FileOut, responses={404: {"model": ErrorOut}})
def get_file(file_id: str, db: Session = Depends(get_db)) -> UploadedFile:
    """Upload metadata and processing status."""
    return get_file_or_404(db, file_id)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsOut,
    responses={
        404: {"model": ErrorOut},
        409: {"model": ErrorOut, "description": "Processing has not finished yet"},
        422: {"model": ErrorOut, "description": "Processing failed"},
    },
)
def get_measurements(file_id: str, db: Session = Depends(get_db)) -> MeasurementsOut:
    """Per-feature area/length plus a summary. Only available once processing has COMPLETED."""
    record = get_file_or_404(db, file_id)
    if record.status in (FileStatus.PENDING, FileStatus.PROCESSING):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"File is {record.status.value}; measurements are not available yet. Retry shortly.",
        )
    if record.status is FileStatus.FAILED:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Processing failed, so there are no measurements: {record.error}",
        )

    summary = summarize((f.measurement_type, f.measurement_status, f.value) for f in record.features)
    return MeasurementsOut(
        file_id=record.id,
        filename=record.filename,
        status=record.status,
        crs=record.crs,
        summary=MeasurementSummaryOut(**vars(summary)),
        measurements=[
            FeatureMeasurementOut(
                feature_index=f.index,
                geometry_type=f.geometry_type,
                measurement_type=f.measurement_type,
                measurement_status=f.measurement_status,
                value=f.value,
                unit=f.unit,
                projected_crs=f.projected_crs,
                note=f.note,
            )
            for f in record.features
        ],
    )
