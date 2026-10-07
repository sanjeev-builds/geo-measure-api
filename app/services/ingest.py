"""Upload validation, storage and safe archive extraction.

Kept free of FastAPI/HTTP concerns: callers map these exceptions to responses.
"""

import unicodedata
import zipfile
from pathlib import Path, PurePath
from typing import BinaryIO

from app.models import FileType

CHUNK_SIZE = 1024 * 1024
MAX_FILENAME_LENGTH = 255

ALLOWED_EXTENSIONS: dict[str, FileType] = {
    ".zip": FileType.SHAPEFILE,
    ".kml": FileType.KML,
}

# Uploads are stored under fixed, server-chosen names. The client's filename is only
# kept as display metadata, so it can never influence a filesystem path.
STORED_FILENAMES: dict[FileType, str] = {
    FileType.SHAPEFILE: "source.zip",
    FileType.KML: "source.kml",
}


class IngestError(Exception):
    """Base class for problems with an uploaded file."""


class UnsupportedFileTypeError(IngestError):
    pass


class EmptyFileError(IngestError):
    pass


class FileTooLargeError(IngestError):
    pass


class InvalidArchiveError(IngestError):
    pass


class MissingShapefileError(IngestError):
    pass


class MissingCompanionFilesError(IngestError):
    pass


# A .shp is unreadable without its index (.shx) and attribute table (.dbf).
# .prj (CRS) and .cpg (encoding) are optional and handled downstream.
REQUIRED_SHAPEFILE_COMPANIONS = (".shx", ".dbf")

# Folders/files added by OS archivers that never contain real data.
_IGNORED_ARCHIVE_PARTS = {"__MACOSX"}


def safe_filename(filename: str | None) -> str:
    """Display name for an upload: no directory components ('../../x.kml' -> 'x.kml'),
    no control characters, bounded length. Never used as a path on disk."""
    # Normalise Windows separators so PurePath handles them on every OS.
    name = PurePath((filename or "").replace("\\", "/")).name
    name = "".join(ch for ch in name if unicodedata.category(ch) != "Cc").strip()
    if len(name) > MAX_FILENAME_LENGTH:
        suffix = PurePath(name).suffix[:16]
        name = name[: MAX_FILENAME_LENGTH - len(suffix)] + suffix
    return name


def stored_upload_path(file_dir: Path, file_type: FileType) -> Path:
    return file_dir / STORED_FILENAMES[file_type]


def detect_file_type(filename: str | None) -> FileType:
    name = safe_filename(filename)
    suffix = PurePath(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UnsupportedFileTypeError(
            f"Unsupported file '{name or '<unnamed>'}'. Allowed extensions: {allowed} "
            "(a Shapefile must be uploaded as a .zip)."
        )
    return ALLOWED_EXTENSIONS[suffix]


def save_upload(source: BinaryIO, destination: Path, max_bytes: int) -> int:
    """Stream an upload to disk in chunks, enforcing a size limit.

    Streaming avoids holding large files in memory. On any failure the partial
    file is removed. Returns the number of bytes written.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with destination.open("wb") as out:
            while chunk := source.read(CHUNK_SIZE):
                written += len(chunk)
                if written > max_bytes:
                    raise FileTooLargeError(f"File exceeds the maximum upload size of {max_bytes} bytes.")
                out.write(chunk)
        if written == 0:
            raise EmptyFileError("Uploaded file is empty.")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return written


def _is_ignored(path: PurePath) -> bool:
    return any(part in _IGNORED_ARCHIVE_PARTS for part in path.parts) or path.name.startswith("._")


def extract_zip(archive_path: Path, destination: Path, *, max_total_bytes: int, max_members: int) -> Path:
    """Safely extract a zip archive and return the extraction directory.

    - Rejects corrupt/non-zip files.
    - Rejects members that would land outside ``destination`` (zip-slip), e.g. '../x' or '/etc/x'.
    - Rejects archives whose declared uncompressed size or member count exceeds the limits (zip bombs).
    The archive is fully validated before anything is written.
    """
    destination = destination.resolve()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > max_members:
                raise InvalidArchiveError(f"Archive has {len(members)} entries; the limit is {max_members}.")
            if sum(m.file_size for m in members) > max_total_bytes:
                raise InvalidArchiveError(f"Archive expands to more than {max_total_bytes} bytes.")

            for member in members:
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(destination):
                    raise InvalidArchiveError(f"Archive entry '{member.filename}' points outside the archive.")

            destination.mkdir(parents=True, exist_ok=True)
            archive.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise InvalidArchiveError("Uploaded .zip is corrupt or not a valid zip archive.") from exc
    except (zipfile.LargeZipFile, NotImplementedError, RuntimeError, EOFError) as exc:
        # NotImplementedError: unsupported compression; RuntimeError: encrypted members; EOFError: truncated data.
        raise InvalidArchiveError(f"Could not extract archive: {exc}") from exc
    return destination


def locate_shapefile(directory: Path) -> Path:
    """Find exactly one .shp in an extracted archive and check its required companion files."""
    candidates = [
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() == ".shp" and not _is_ignored(path.relative_to(directory))
    ]
    if not candidates:
        raise MissingShapefileError("No .shp file found in the uploaded archive.")
    if len(candidates) > 1:
        names = ", ".join(sorted(str(p.relative_to(directory).as_posix()) for p in candidates))
        raise MissingShapefileError(f"Archive contains more than one .shp file ({names}); upload one per archive.")

    shp = candidates[0]
    # Match companions case-insensitively: archives often mix 'parcels.SHP' with 'parcels.dbf'.
    siblings = {p.name.lower() for p in shp.parent.iterdir() if p.is_file()}
    missing = [ext for ext in REQUIRED_SHAPEFILE_COMPANIONS if f"{shp.stem.lower()}{ext}" not in siblings]
    if missing:
        raise MissingCompanionFilesError(
            f"Shapefile '{shp.name}' is missing required companion file(s): "
            + ", ".join(f"{shp.stem}{ext}" for ext in missing)
            + "."
        )
    return shp
