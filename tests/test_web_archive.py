"""Tests for web.archive: listing and HTTP Range parsing."""

import pytest

from web.archive import ArchiveStore, parse_range_header


@pytest.fixture
def archive(host_root):
    creator_dir = host_root / "archive" / "Creator"
    creator_dir.mkdir(parents=True)
    (creator_dir / "#Creator 2026-03-06 Title.mp4").write_bytes(b"x" * 100)
    (creator_dir / "20260306_120000_#Creator raw.ts").write_bytes(b"y" * 50)
    (creator_dir / "notes.txt").write_bytes(b"ignore me")
    return ArchiveStore(host_root / "archive")


def test_list_recordings_includes_mp4_and_ts_only(archive):
    names = {item.filename for item in archive.list_recordings()}
    assert names == {"#Creator 2026-03-06 Title.mp4", "20260306_120000_#Creator raw.ts"}


def test_list_recordings_marks_raw_and_merging(archive):
    merging = archive.root / "Creator" / "x.merging.mp4"
    merging.write_bytes(b"z" * 10)
    by_name = {item.filename: item for item in archive.list_recordings()}
    assert by_name["20260306_120000_#Creator raw.ts"].is_raw is True
    assert by_name["#Creator 2026-03-06 Title.mp4"].is_raw is False
    assert by_name["x.merging.mp4"].is_merging is True


def test_list_recordings_sorted_newest_first(archive):
    import os
    import time

    older = archive.root / "Creator" / "older.mp4"
    older.write_bytes(b"o")
    now = time.time()
    os.utime(older, (now - 5000, now - 5000))
    items = archive.list_recordings()
    assert items[0].filename != "older.mp4"


def test_list_recordings_missing_root(tmp_path):
    assert ArchiveStore(tmp_path / "nope").list_recordings() == []


def test_iter_file_returns_requested_byte_range(archive):
    path = archive.root / "Creator" / "#Creator 2026-03-06 Title.mp4"
    data = b"".join(archive.iter_file(path, 10, 5))
    assert data == b"xxxxx"


def test_guess_media_type(archive):
    assert archive.guess_media_type("a.mp4") == "video/mp4"
    assert archive.guess_media_type("a.unknown") == "application/octet-stream"


# ---------- range parsing ----------


def test_range_absent_returns_none():
    assert parse_range_header("", 100) is None


@pytest.mark.parametrize(
    "header,expected",
    [
        ("bytes=0-9", (0, 9)),
        ("bytes=10-", (10, 99)),
        ("bytes=-10", (90, 99)),
        ("bytes=0-1000", (0, 99)),
    ],
)
def test_range_parsing(header, expected):
    assert parse_range_header(header, 100) == expected


@pytest.mark.parametrize("header", ["items=0-9", "bytes=abc", "bytes=", "bytes=5-1x"])
def test_range_unparseable_falls_back_to_full_file(header):
    assert parse_range_header(header, 100) is None


@pytest.mark.parametrize("header", ["bytes=100-", "bytes=200-300", "bytes=50-40"])
def test_range_unsatisfiable_raises(header):
    with pytest.raises(ValueError):
        parse_range_header(header, 100)


def test_range_suffix_zero_raises():
    with pytest.raises(ValueError):
        parse_range_header("bytes=-0", 100)
