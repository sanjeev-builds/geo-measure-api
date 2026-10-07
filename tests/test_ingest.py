import pytest

from app.services.ingest import (
    InvalidArchiveError,
    MissingCompanionFilesError,
    MissingShapefileError,
    extract_zip,
    locate_shapefile,
)
from tests.factories import make_zip

LIMITS = {"max_total_bytes": 10 * 1024 * 1024, "max_members": 50}


def write_zip(tmp_path, entries: dict[str, bytes]):
    path = tmp_path / "upload.zip"
    path.write_bytes(make_zip(entries))
    return path


def touch_all(directory, *names):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(b"x")
    return directory


class TestExtractZip:
    def test_extracts_members(self, tmp_path):
        archive = write_zip(tmp_path, {"a.shp": b"1", "folder/b.dbf": b"2"})

        out = extract_zip(archive, tmp_path / "out", **LIMITS)

        assert (out / "a.shp").read_bytes() == b"1"
        assert (out / "folder" / "b.dbf").read_bytes() == b"2"

    @pytest.mark.parametrize("member", ["../evil.txt", "folder/../../evil.txt", "/abs/evil.txt"])
    def test_rejects_path_traversal_without_writing_anything(self, tmp_path, member):
        archive = write_zip(tmp_path, {"ok.txt": b"fine", member: b"evil"})

        with pytest.raises(InvalidArchiveError, match="outside the archive"):
            extract_zip(archive, tmp_path / "out", **LIMITS)

        assert not (tmp_path / "out").exists()
        assert not (tmp_path / "evil.txt").exists()

    def test_rejects_non_zip_bytes(self, tmp_path):
        archive = tmp_path / "upload.zip"
        archive.write_bytes(b"this is definitely not a zip file")

        with pytest.raises(InvalidArchiveError, match="corrupt or not a valid zip"):
            extract_zip(archive, tmp_path / "out", **LIMITS)

    def test_rejects_truncated_zip(self, tmp_path):
        data = make_zip({"a.shp": b"x" * 5000, "a.dbf": b"y" * 5000})
        archive = tmp_path / "upload.zip"
        archive.write_bytes(data[: len(data) // 2])

        with pytest.raises(InvalidArchiveError):
            extract_zip(archive, tmp_path / "out", **LIMITS)

    def test_rejects_archive_with_too_many_members(self, tmp_path):
        archive = write_zip(tmp_path, {f"f{i}.txt": b"x" for i in range(5)})

        with pytest.raises(InvalidArchiveError, match="entries"):
            extract_zip(archive, tmp_path / "out", max_total_bytes=10_000, max_members=4)

    def test_rejects_archive_that_expands_too_far(self, tmp_path):
        # Highly compressible: tiny on disk, large once extracted (zip-bomb shape).
        archive = write_zip(tmp_path, {"big.txt": b"0" * 100_000})

        with pytest.raises(InvalidArchiveError, match="expands"):
            extract_zip(archive, tmp_path / "out", max_total_bytes=10_000, max_members=10)


class TestLocateShapefile:
    def test_finds_shapefile_with_companions(self, tmp_path):
        directory = touch_all(tmp_path, "parcels.shp", "parcels.shx", "parcels.dbf")

        assert locate_shapefile(directory) == directory / "parcels.shp"

    def test_finds_shapefile_in_nested_folder(self, tmp_path):
        directory = touch_all(tmp_path, "data/v1/parcels.shp", "data/v1/parcels.shx", "data/v1/parcels.dbf")

        assert locate_shapefile(directory) == directory / "data" / "v1" / "parcels.shp"

    def test_companion_matching_is_case_insensitive(self, tmp_path):
        directory = touch_all(tmp_path, "PARCELS.SHP", "parcels.shx", "Parcels.DBF")

        assert locate_shapefile(directory).name == "PARCELS.SHP"

    def test_missing_shp(self, tmp_path):
        directory = touch_all(tmp_path, "parcels.shx", "parcels.dbf", "readme.txt")

        with pytest.raises(MissingShapefileError, match="No .shp file"):
            locate_shapefile(directory)

    @pytest.mark.parametrize(
        ("present", "missing"),
        [
            (("parcels.shp", "parcels.dbf"), "parcels.shx"),
            (("parcels.shp", "parcels.shx"), "parcels.dbf"),
        ],
    )
    def test_missing_companion(self, tmp_path, present, missing):
        directory = touch_all(tmp_path, *present)

        with pytest.raises(MissingCompanionFilesError, match=missing):
            locate_shapefile(directory)

    def test_reports_all_missing_companions(self, tmp_path):
        directory = touch_all(tmp_path, "parcels.shp")

        with pytest.raises(MissingCompanionFilesError, match=r"parcels\.shx, parcels\.dbf"):
            locate_shapefile(directory)

    def test_companions_in_a_different_folder_do_not_count(self, tmp_path):
        directory = touch_all(tmp_path, "a/parcels.shp", "b/parcels.shx", "b/parcels.dbf")

        with pytest.raises(MissingCompanionFilesError):
            locate_shapefile(directory)

    def test_multiple_shapefiles_are_rejected(self, tmp_path):
        directory = touch_all(tmp_path, "a.shp", "a.shx", "a.dbf", "b.shp", "b.shx", "b.dbf")

        with pytest.raises(MissingShapefileError, match="more than one"):
            locate_shapefile(directory)

    def test_macos_resource_forks_are_ignored(self, tmp_path):
        directory = touch_all(
            tmp_path, "parcels.shp", "parcels.shx", "parcels.dbf", "__MACOSX/._parcels.shp", "._parcels.shp"
        )

        assert locate_shapefile(directory) == directory / "parcels.shp"
