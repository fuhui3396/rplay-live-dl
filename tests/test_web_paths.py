"""Tests for web.paths: traversal protection is the security boundary."""

import pytest

from web.paths import resolve_within


def test_resolve_simple_name_inside_root(tmp_path):
    assert resolve_within(tmp_path, "creator") == tmp_path / "creator"


def test_resolve_nested_parts(tmp_path):
    assert resolve_within(tmp_path, "creator", "video.mp4") == (
        tmp_path / "creator" / "video.mp4"
    )


@pytest.mark.parametrize(
    "part", ["..", "../etc", "a/b", "a\\b", ".", "", ".hidden", "/abs"]
)
def test_rejects_traversal_and_separators(tmp_path, part):
    assert resolve_within(tmp_path, part) is None


def test_rejects_nested_traversal(tmp_path):
    assert resolve_within(tmp_path, "creator", "..", "..", "etc") is None


def test_rejects_symlink_escaping_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside)
    assert resolve_within(root, "link", "secret.txt") is None


def test_allows_symlink_staying_inside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "real").mkdir()
    (root / "link").symlink_to(root / "real")
    assert resolve_within(root, "link") == root / "real"


def test_absolute_path_part_rejected(tmp_path):
    assert resolve_within(tmp_path, "/etc/passwd") is None
