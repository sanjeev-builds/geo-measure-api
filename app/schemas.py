from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import FileStatus, FileType, MeasurementStatus, MeasurementType


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    file_type: FileType
    feature_count: int | None
    crs: str | None
    status: FileStatus
    error: str | None
    created_at: datetime
    processed_at: datetime | None


class FeatureMeasurementOut(BaseModel):
    feature_index: int
    geometry_type: str | None
    measurement_type: MeasurementType
    measurement_status: MeasurementStatus
    value: float | None
    unit: str | None
    projected_crs: str | None = Field(description="CRS the geometry was measured in")
    note: str | None


class MeasurementSummaryOut(BaseModel):
    feature_count: int
    measured_count: int
    not_applicable_count: int = Field(description="Features with nothing to measure, e.g. points")
    unavailable_count: int = Field(description="Features that should have been measured but could not be")
    total_area_m2: float | None = Field(description="null if a feature that could add area was not measured")
    total_length_m: float | None = Field(description="null if a feature that could add length was not measured")
    totals_complete: bool
    note: str | None


class MeasurementsOut(BaseModel):
    file_id: str
    filename: str
    status: FileStatus
    crs: str | None
    summary: MeasurementSummaryOut
    measurements: list[FeatureMeasurementOut]


class HealthOut(BaseModel):
    status: str
    database: str


class ErrorOut(BaseModel):
    detail: str
