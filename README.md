# Geospatial File Measurement API

A FastAPI service that accepts a **KML** file or a **zipped Shapefile**, extracts its features, and calculates
**polygon area (m²)** and **line length (m)**. Geometries are transformed into a projected CRS before measuring,
because area and length calculated directly on latitude/longitude degrees are meaningless.

Built for the Aereo SDE Intern assignment.

## Contents

- [Running it](#running-it)
- [API](#api)
- [How it works](#how-it-works)
- [Design decisions](#design-decisions)
- [Testing](#testing)
- [Known limitations](#known-limitations)
- [What I learned](#what-i-learned)
- [Future improvements](#future-improvements)

## Running it

Requires Python 3.12. GDAL, GEOS and PROJ come bundled inside the `pyogrio`, `shapely` and `pyproj` wheels,
so nothing needs to be installed system-wide.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Interactive docs are at http://localhost:8000/docs. Run the tests with `pytest`.

With Docker:

```bash
docker build -t geo-measure-api .
docker run -p 8000:8000 -v geo-data:/app/data geo-measure-api
```

Settings can be overridden with environment variables:

| Variable | Default | |
|---|---|---|
| `GEO_DATABASE_URL` | `sqlite:///./data/app.db` | any SQLAlchemy URL |
| `GEO_UPLOAD_DIR` | `./data/uploads` | where uploads are stored |
| `GEO_MAX_UPLOAD_BYTES` | 50 MB | upload size limit |
| `GEO_MAX_EXTRACTED_BYTES` | 500 MB | zip-bomb guard: max uncompressed size of an archive |
| `GEO_MAX_ARCHIVE_MEMBERS` | 1000 | zip-bomb guard: max entries in an archive |

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/files/` | Upload a `.kml` or `.zip` (Shapefile). Returns `202` and queues processing |
| `GET` | `/api/files/{id}/` | File info and processing status |
| `GET` | `/api/files/{id}/measurements/` | Per-feature measurements and a summary |
| `GET` | `/health` | Liveness check, including the database |

`samples/` contains a small KML and a Shapefile zip (near Bangalore) to try it with.

### Upload

```bash
curl -F "file=@samples/survey.kml" http://localhost:8000/api/files/
```

```json
{
  "id": "57397f66e9ca4785b1e009b8b32ac4c7",
  "filename": "survey.kml",
  "file_type": "KML",
  "feature_count": null,
  "crs": null,
  "status": "PENDING",
  "error": null,
  "created_at": "2026-10-07T14:29:54.995183Z",
  "processed_at": null
}
```

Errors: `400` for an unsupported extension or an empty file, `413` if the file is too large.
Problems *inside* the file, such as a corrupt zip or a missing `.dbf`, are found during processing.
They set the status to `FAILED` and store a readable `error`.

### File information

```bash
curl http://localhost:8000/api/files/57397f66e9ca4785b1e009b8b32ac4c7/
```

```json
{
  "id": "57397f66e9ca4785b1e009b8b32ac4c7",
  "filename": "survey.kml",
  "file_type": "KML",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error": null,
  "created_at": "2026-10-07T14:29:54.995183Z",
  "processed_at": "2026-10-07T14:29:55.122612Z"
}
```

`status` moves `PENDING → PROCESSING → COMPLETED`, or to `FAILED` with `error` set. Unknown id → `404`.

### Measurements

```bash
curl http://localhost:8000/api/files/57397f66e9ca4785b1e009b8b32ac4c7/measurements/
```

```json
{
  "file_id": "57397f66e9ca4785b1e009b8b32ac4c7",
  "filename": "survey.kml",
  "status": "COMPLETED",
  "crs": "EPSG:4326",
  "summary": {
    "feature_count": 3,
    "measured_count": 2,
    "not_applicable_count": 1,
    "unavailable_count": 0,
    "total_area_m2": 1201683.92,
    "total_length_m": 3100.84,
    "totals_complete": true,
    "note": null
  },
  "measurements": [
    { "feature_index": 0, "geometry_type": "Polygon", "measurement_type": "AREA",
      "measurement_status": "MEASURED", "value": 1201683.92, "unit": "m²",
      "projected_crs": "EPSG:32643", "note": null },
    { "feature_index": 1, "geometry_type": "Point", "measurement_type": "NONE",
      "measurement_status": "NOT_APPLICABLE", "value": null, "unit": null,
      "projected_crs": null, "note": "Point geometries have no area or length." },
    { "feature_index": 2, "geometry_type": "LineString", "measurement_type": "LENGTH",
      "measurement_status": "MEASURED", "value": 3100.84, "unit": "m",
      "projected_crs": "EPSG:32643", "note": null }
  ]
}
```

(Values rounded here; the API returns full floats.)

Each feature gets a `measurement_status`:

- `MEASURED`: a value in `m²` or `m`, plus the CRS it was measured in.
- `NOT_APPLICABLE`: points; there is nothing to measure.
- `UNAVAILABLE`: it should have been measured but couldn't be. Possible causes are no CRS in the file, an
  unsupported geometry type, an empty geometry, or a polar feature. The `note` says which.

If any feature that could have contributed to a total is `UNAVAILABLE`, that total is `null` and
`totals_complete` is `false`. I'd rather return no total than a number that looks complete but isn't.

| Situation | Response |
|---|---|
| Still `PENDING` / `PROCESSING` | `409` with the current status |
| Processing `FAILED` | `422` with the stored error |
| Unknown id | `404` |

## How it works

### Layout

```
app/
  main.py            app factory, lifespan (creates tables), /health
  config.py          settings (pydantic-settings, GEO_* env vars)
  db.py              engine, session factory, get_db dependency
  models.py          UploadedFile, Feature
  schemas.py         response models
  middleware.py      rejects oversized requests by Content-Length
  api/files.py       the three /api/files endpoints
  services/
    ingest.py        upload validation, storage, safe zip extraction, finding the .shp
    reader.py        GDAL (pyogrio) → features with geometry, properties, CRS
    crs.py           choosing the projected CRS, transforming geometries
    measure.py       area/length per feature, summary totals
    processor.py     background job tying the above together
tests/               140 tests; sample files are generated in code (tests/factories.py)
```

The services know nothing about HTTP. They raise their own exceptions, and the API layer maps those to status codes.

### File processing

```
POST /api/files/
  check extension (.kml / .zip) and size → save as uploads/<id>/source.<ext> → row with status PENDING → 202
        │  (BackgroundTasks)
        ▼
process_file(id): status = PROCESSING
  .zip: reject corrupt archives, entries that escape the folder (zip-slip),
        and archives over the size/entry limits → extract
        → exactly one .shp, with its .shx and .dbf next to it
  .kml: used as-is
  → read every layer with GDAL → for each feature: index, geometry type, geometry, properties, CRS
  → measure each feature → save Feature rows → status = COMPLETED
  (any known problem → FAILED with a message; the extracted copy is deleted either way)
```

Some details:

- **The client's filename is never used as a path.** Uploads are saved as `source.zip` / `source.kml`, and the
  original name is only kept for display. An early version used it directly. Names like `a<b>.kml` caused 500s,
  and `evil:stream.kml` created an NTFS alternate data stream.
- **A missing `.prj` doesn't fail the file.** Features are still extracted, but they can't be measured (see below).
- **KML**: LIBKML turns each `<Folder>` into a separate layer, so all layers are read. Google Earth display
  fields (`tessellate`, `extrude`, `icon`, …) are dropped from properties because they describe styling, not
  data. `<ExtendedData>` values are kept. They come back as strings, which is how KML stores them.
- **GDAL error messages** include the absolute server path. That's replaced with the file name before the error
  is stored.

### CRS handling

`services/crs.py` chooses the CRS a feature is measured in:

1. **No CRS** (e.g. a Shapefile without `.prj`) → no measurement. I deliberately **don't assume EPSG:4326**.
   If the data is actually in UTM or a state plane, assuming WGS84 gives confident-looking wrong numbers.
2. **Projected CRS** → measure in it directly, converting its unit to metres using pyproj's unit factor
   (e.g. ×0.3048006 for US survey feet).
   *Exception:* Mercator, Web Mercator (EPSG:3857) and Plate Carrée are projected but distort area heavily away
   from the equator. Web Mercator overstates area about **4×** at 60°N. These are treated like geographic input.
3. **Geographic CRS** (lon/lat) → pick a UTM zone for the feature:
   - transform the feature to WGS84 and take its bounding box;
   - check the coordinates are valid lon/lat (if not, the CRS is probably mislabelled), between 80°S and 84°N
     where UTM is defined, and no wider than 6° of longitude, i.e. one zone;
   - ask pyproj's CRS database (`query_utm_crs_info`) for the WGS84 UTM zone containing the box's centre,
     e.g. Bangalore → EPSG:32643 (UTM 43N). On a zone boundary or the equator two zones match, and the lowest
     EPSG code is used so the result is deterministic.

The geometry is then transformed with a pyproj `Transformer` (`always_xy=True`, so coordinates are always lon, lat).

Each feature gets its own zone, so one file can contain features measured in different UTM zones. Every value
is already in metres, so adding them up for the total is still valid.

### Measurement

For each feature in `services/measure.py`:

1. Classify the geometry:
   - Polygon / MultiPolygon → area;
   - LineString / MultiLineString / LinearRing → length;
   - Point / MultiPoint → not applicable;
   - anything else, e.g. GeometryCollection → unavailable, with a note.
2. Drop Z values; measurement is 2D.
3. If a polygon is invalid, repair it with `shapely.make_valid(method="structure")` and record what was wrong in
   the note. This matters: a self-intersecting "bowtie" polygon has a raw shapely area of **0**.
4. Choose the CRS (above), transform, and take `.area` / `.length` multiplied by the unit factor.

UTM's scale error inside a zone is about 0.1% for length and 0.2% for area. For the sample Bangalore square,
UTM gives 1,201,684 m² and a geodesic calculation on the WGS84 ellipsoid gives 1,200,290 m², a 0.12% difference.

## Design decisions

**FastAPI over Django REST Framework.** The service has three endpoints and no admin, auth or templates, so
Django's extras weren't needed. FastAPI gives request validation and response models through Pydantic, plus
OpenAPI docs at `/docs`, with very little code. `BackgroundTasks` is also built in.

**GDAL via pyogrio for reading.** One library reads both Shapefile and KML, including multi-layer KML and the
CRS from `.prj` files. I considered `pyshp` + `fastkml`, which would mean two parsers and handling `.prj` WKT
myself.

**UTM for measurement.** It's the standard choice for local survey-scale data like Aereo's drone surveys, and it's
easy to explain. Alternatives I considered:
- a single global equal-area CRS (e.g. EPSG:6933): good for area but distorts length, and is less precise
  locally;
- geodesic calculations directly on the ellipsoid (`pyproj.Geod`): the most accurate, but it doesn't meet the
  brief's "transform to a projected CRS" requirement.

I used Geod in the tests instead, as an independent check that the UTM numbers are right.

**SQLite.** Zero setup for a reviewer. Everything goes through SQLAlchemy, so switching to Postgres is a
`GEO_DATABASE_URL` change. Geometry is stored as GeoJSON in a JSON column: the API returns measurements rather
than running spatial queries, so PostGIS wasn't needed yet.

**BackgroundTasks for processing.** The upload returns `202` immediately and the client polls the status, which
is the right API shape for slow work. BackgroundTasks runs the job **in the same process** after the response
is sent. It is **not a durable queue**: if the server restarts mid-job, the file stays in `PROCESSING`. For this
assignment that trade-off is fine. A real deployment would use Celery or RQ with Redis, or a jobs table that
workers claim.

**Measuring during processing, not on each request.** Results are calculated once and stored next to the
features, so `GET /measurements/` is a plain read. The downside: if the measurement logic changes, existing files
need re-processing.

**Status codes.** `202` for upload because the work happens later. `409` when measurements are requested before
processing finishes, because the resource exists but isn't in the right state yet. `422` when processing failed,
because the uploaded content couldn't be processed. (FastAPI also uses 422 for request validation errors; those
have a list in `detail` instead of a string.)

## Testing

```bash
pytest     # 140 tests, ~7 s
```

- **Measurement correctness**: expected values come from `pyproj.Geod`, not from UTM, so the tests check
  against a different method rather than against the code itself. Cases include polygons and lines in
  EPSG:4326, multi-geometries, holes, southern hemisphere, NAD83, US-foot state plane, Web Mercator input, and
  3D coordinates.
- **CRS selection**: UTM zones for several cities, zone boundaries, the equator, world projections, and a
  missing CRS.
- **Edge cases**: points, empty geometries, GeometryCollections, invalid (bowtie) polygons, polar and very wide
  features, mislabelled CRS.
- **File handling**: zip-slip, corrupt and truncated zips, zip bombs, missing `.shp`/`.shx`/`.dbf`, multiple
  `.shp` files, nested folders, macOS `__MACOSX` entries, hostile filenames, size limits, and that error
  messages contain no server paths.
- **Lifecycle and API**: PENDING → PROCESSING → COMPLETED/FAILED, re-processing without duplicates, and every
  endpoint's success and error responses.

Test Shapefiles and KML are generated in code (`tests/factories.py`), so the repo has no binary fixtures.

## Known limitations

- Processing is in-process (see BackgroundTasks above). A restart leaves jobs stuck in `PROCESSING`, and there
  is no retry endpoint.
- Features near the poles, or wider than 6° of longitude, are not measured. They get a note explaining why.
  Features crossing the 180° meridian are also rejected, under the "too wide" message.
- A feature that crosses a UTM zone boundary is measured in the zone of its centre. The extra error is small,
  under ~0.5%.
- Projected CRSs other than the world cylindrical ones are trusted as-is. The assumption is that whoever chose
  the CRS chose one suited to the data's region.
- Uploads are kept forever; there's no retention policy or delete endpoint.
- `/measurements/` returns every feature, with no pagination.
- SQLite allows one writer at a time, which is fine for a single instance but not for many workers.
- Shapefiles without a `.cpg` are read with GDAL's default encoding, so non-Latin attribute text may come out
  garbled.

## What I learned

- **"Projected" doesn't mean "safe to measure in".** I assumed any metre-based CRS was fine until I tested Web
  Mercator at 60°N and got roughly 4× the true area.
- **Not guessing is a valid design choice.** Treating a missing `.prj` as WGS84 is common, but if the
  coordinates are actually in metres the output is confidently wrong. Returning "unavailable, here's why" is
  more honest.
- **Ring orientation matters.** My first geodesic test reference *added* a hole's area instead of subtracting it,
  because `Geod` decides from ring winding direction. The service was right and the test was wrong; it only
  showed up because the reference and the implementation used different methods.
- **Framework internals affect security.** Starlette writes the whole multipart body to disk before my endpoint
  runs, so a size check inside the endpoint alone doesn't stop a huge upload. GDAL's error messages contained
  absolute server paths. User-supplied filenames behave differently on Windows (`:`, `<`, reserved names).
- **Shapefiles hold one geometry type per file.** My first test fixture mixed polygons, lines and points in one
  Shapefile and GDAL refused to write it.
- **Long paths on Windows**: the compiled extensions (GDAL, GEOS, SQLAlchemy) failed to load from a very deep
  directory, so the project had to move to a short path.

## Future improvements

- A durable job queue (Celery/RQ + Redis), plus retry of failed files.
- PostGIS for spatial storage and queries, with Alembic migrations instead of `create_all`.
- Store uploads in object storage (S3) with a retention policy.
- Authentication and per-user files.
- Pagination for measurements; GeoJSON output of features.
- Geodesic measurement as a fallback for polar or very large features, instead of returning "unavailable".
- More formats (GeoJSON, GeoPackage) — GDAL already supports them, so it's mostly validation work.
- Polygon perimeter, and a choice of output units (hectares, km).

---

AI coding assistants were used during development, as the assignment allows.
