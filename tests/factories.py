"""Build sample geospatial files in code, so the repo needs no binary fixtures."""

import io
import warnings
import zipfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, MultiPolygon, Polygon, box

# Roughly 1.1 km x 1.1 km near Bangalore, in lon/lat.
BANGALORE_SQUARE = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])

SAMPLE_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>Survey</name>
  <Placemark>
    <name>Plot A</name>
    <description>Farm plot</description>
    <ExtendedData>
      <Data name="owner"><value>Ravi</value></Data>
      <Data name="survey_no"><value>42</value></Data>
    </ExtendedData>
    <Polygon><outerBoundaryIs><LinearRing>
      <coordinates>77.59,12.97,0 77.60,12.97,0 77.60,12.98,0 77.59,12.98,0 77.59,12.97,0</coordinates>
    </LinearRing></outerBoundaryIs></Polygon>
  </Placemark>
  <Placemark>
    <name>Well</name>
    <Point><coordinates>77.595,12.975</coordinates></Point>
  </Placemark>
  <Folder>
    <name>Roads</name>
    <Placemark>
      <name>Access road</name>
      <LineString><coordinates>77.59,12.97 77.61,12.99</coordinates></LineString>
    </Placemark>
  </Folder>
</Document>
</kml>
"""


def sample_frame(crs: str | None = "EPSG:4326") -> gpd.GeoDataFrame:
    """Three polygon features. A shapefile layer holds a single geometry type, so no mixing here."""
    return gpd.GeoDataFrame(
        {
            "name": ["Plot A", "Plot B", "Plot C"],
            "owner": ["Ravi", None, "Asha"],
            "survey_no": [42, 7, 3],
        },
        geometry=[
            BANGALORE_SQUARE,
            box(77.61, 12.97, 77.615, 12.975),
            MultiPolygon([box(77.62, 12.97, 77.621, 12.971), box(77.63, 12.97, 77.631, 12.971)]),
        ],
        crs=crs,
    )


def line_frame(crs: str | None = "EPSG:4326") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": ["Access road"]}, geometry=[LineString([(77.59, 12.97), (77.61, 12.99)])], crs=crs)


def write_shapefile(directory: Path, frame: gpd.GeoDataFrame, stem: str = "parcels") -> dict[str, bytes]:
    """Write a shapefile and return its component files as {filename: bytes}."""
    directory.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        # pyogrio warns when writing without a CRS; the no-.prj tests do that on purpose.
        warnings.filterwarnings("ignore", message="'crs' was not provided")
        frame.to_file(directory / f"{stem}.shp", driver="ESRI Shapefile", engine="pyogrio")
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.stem == stem}


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def shapefile_zip(
    directory: Path,
    frame: gpd.GeoDataFrame | None = None,
    *,
    exclude: tuple[str, ...] = (),
    prefix: str = "",
) -> bytes:
    """Zip a shapefile, optionally dropping components (e.g. exclude=('.prj',)) or nesting it in a folder."""
    files = write_shapefile(directory, sample_frame() if frame is None else frame)
    kept = {f"{prefix}{name}": data for name, data in files.items() if Path(name).suffix.lower() not in exclude}
    return make_zip(kept)
