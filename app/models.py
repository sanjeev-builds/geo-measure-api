import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Stores UTC and always returns timezone-aware datetimes (SQLite drops tzinfo on read)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        return value.astimezone(UTC) if value is not None and value.tzinfo else value

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class FileType(enum.StrEnum):
    SHAPEFILE = "SHAPEFILE"
    KML = "KML"


class FileStatus(enum.StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MeasurementType(enum.StrEnum):
    AREA = "AREA"
    LENGTH = "LENGTH"
    NONE = "NONE"


class MeasurementStatus(enum.StrEnum):
    MEASURED = "MEASURED"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # e.g. points: nothing to measure
    UNAVAILABLE = "UNAVAILABLE"  # should/could be measured but wasn't (no CRS, unsupported type, ...)


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[FileType] = mapped_column(Enum(FileType, native_enum=False, length=16))
    status: Mapped[FileStatus] = mapped_column(
        Enum(FileStatus, native_enum=False, length=16), default=FileStatus.PENDING
    )
    crs: Mapped[str | None] = mapped_column(Text)  # "EPSG:xxxx", or WKT when no authority code matches
    feature_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    features: Mapped[list["Feature"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", order_by="Feature.index"
    )


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (UniqueConstraint("file_id", "index", name="uq_feature_file_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    geometry_type: Mapped[str | None] = mapped_column(String(32))
    geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # GeoJSON, source CRS
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    crs: Mapped[str | None] = mapped_column(Text)

    measurement_type: Mapped[MeasurementType] = mapped_column(
        Enum(MeasurementType, native_enum=False, length=16), default=MeasurementType.NONE
    )
    measurement_status: Mapped[MeasurementStatus] = mapped_column(Enum(MeasurementStatus, native_enum=False, length=16))
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(16))
    projected_crs: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)

    file: Mapped[UploadedFile] = relationship(back_populates="features")
