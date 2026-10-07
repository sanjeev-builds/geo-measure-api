"""Read features out of a Shapefile or KML using GDAL (via pyogrio).

This layer only extracts what the file says: geometry, attributes and the CRS the
file declares. It never assigns a CRS and does no measurement.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyogrio
from pyogrio.errors import DataLayerError, DataSourceError
from shapely.geometry.base import BaseGeometry

from app.models import FileType
from app.services.crs import crs_to_string

# LIBKML exposes Google Earth rendering settings as columns. They describe how a
# placemark is drawn, not what it is, so they are not treated as attributes.
KML_DISPLAY_FIELDS = frozenset({"altitudeMode", "tessellate", "extrude", "visibility", "drawOrder", "icon"})


class ReaderError(Exception):
    """The file could not be read as geospatial data."""


@dataclass(frozen=True)
class ExtractedFeature:
    index: int
    geometry: BaseGeometry | None
    properties: dict[str, Any] = field(default_factory=dict)
    crs: str | None = None

    @property
    def geometry_type(self) -> str | None:
        return self.geometry.geom_type if self.geometry is not None else None


@dataclass(frozen=True)
class ReadResult:
    crs: str | None
    features: list[ExtractedFeature]


def read_features(path: Path, file_type: FileType) -> ReadResult:
    if file_type is FileType.KML:
        return _read_kml(path)
    return _read_shapefile(path)


def _read_shapefile(path: Path) -> ReadResult:
    frame = _read_layer(path, layer=None)
    # frame.crs is None when the archive has no .prj; we leave it that way on purpose.
    crs = crs_to_string(frame.crs) if frame.crs else None
    return ReadResult(crs=crs, features=list(_to_features(frame, crs, start_index=0)))


def _read_kml(path: Path) -> ReadResult:
    # LIBKML maps each KML <Folder> to its own layer, so read all of them.
    try:
        layers = [name for name, _geometry_type in pyogrio.list_layers(path)]
    except (DataSourceError, DataLayerError) as exc:
        raise ReaderError(f"Could not read KML: {_gdal_message(exc, path)}") from exc

    features: list[ExtractedFeature] = []
    crs_values: set[str | None] = set()
    for layer in layers:
        frame = _read_layer(path, layer=layer)
        if frame.empty:
            continue
        crs = crs_to_string(frame.crs) if frame.crs else None
        crs_values.add(crs)
        frame = frame.drop(columns=[c for c in frame.columns if c in KML_DISPLAY_FIELDS])
        features.extend(_to_features(frame, crs, start_index=len(features)))

    # The CRS comes from the driver (KML 2.2 mandates WGS84 lon/lat); report only what it declares.
    file_crs = crs_values.pop() if len(crs_values) == 1 else None
    return ReadResult(crs=file_crs, features=features)


def _read_layer(path: Path, layer: str | None):
    try:
        return pyogrio.read_dataframe(path, layer=layer)
    except (DataSourceError, DataLayerError) as exc:
        raise ReaderError(f"Could not read '{path.name}': {_gdal_message(exc, path)}") from exc


def _to_features(frame, crs: str | None, start_index: int):
    geometry_column = frame.geometry.name
    attribute_columns = [c for c in frame.columns if c != geometry_column]
    rows = frame[attribute_columns].itertuples(index=False)
    for offset, (geometry, row) in enumerate(zip(frame.geometry, rows, strict=True)):
        properties = {
            column: value
            for column, raw in zip(attribute_columns, row, strict=True)
            if (value := to_json_value(raw)) is not None
        }
        yield ExtractedFeature(
            index=start_index + offset,
            geometry=geometry if isinstance(geometry, BaseGeometry) else None,
            properties=properties,
            crs=crs,
        )


def to_json_value(value: Any) -> Any:
    """Convert pandas/numpy cell values into JSON-serialisable Python values (None for missing)."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _gdal_message(exc: Exception, path: Path) -> str:
    """First line of a GDAL error, with the server-side path replaced by the bare file name.

    GDAL embeds the absolute path it opened; that must not reach API clients.
    """
    message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    for form in {str(path), str(path.resolve()), path.as_posix(), path.resolve().as_posix()}:
        message = message.replace(form, path.name)
    return message
