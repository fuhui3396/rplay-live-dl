"""Tests for web.credentials and web.logs."""

import json
import os
import time

import pytest
import requests

from web.credentials import check_credentials
from web.logs import read_logs


def _response(status_code=200, payload=None):
    response = requests.Response()
    response.status_code = status_code
    if payload is None:
        response._content = b"{}"
    else:
        response._content = json.dumps(payload).encode()
    return response


def _patch_post(monkeypatch, handler):
    monkeypatch.setattr(requests, "post", handler)


def test_check_credentials_success(monkeypatch):
    _patch_post(
        monkeypatch,
        lambda *a, **k: _response(200, {"accessToken": "jwt-token"}),
    )
    result = check_credentials("https://api.rplay.live", "123", "refresh")
    assert result.ok is True


def test_check_credentials_missing_user_oid():
    result = check_credentials("https://api.rplay.live", "", "refresh")
    assert result.ok is False
    assert "USER_OID" in result.title


def test_check_credentials_missing_token():
    result = check_credentials("https://api.rplay.live", "123", "")
    assert result.ok is False
    assert "REFRESH_TOKEN" in result.title


@pytest.mark.parametrize("status", [401, 403])
def test_check_credentials_rejected(monkeypatch, status):
    _patch_post(monkeypatch, lambda *a, **k: _response(status))
    result = check_credentials("https://api.rplay.live", "123", "bad")
    assert result.ok is False
    assert str(status) in result.detail


def test_check_credentials_server_error(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _response(503))
    result = check_credentials("https://api.rplay.live", "123", "tok")
    assert result.ok is False
    assert "服务器错误" in result.title


def test_check_credentials_missing_access_token(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _response(200, {"nope": 1}))
    result = check_credentials("https://api.rplay.live", "123", "tok")
    assert result.ok is False
    assert "accessToken" in result.title


def test_check_credentials_timeout(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.Timeout()

    _patch_post(monkeypatch, boom)
    result = check_credentials("https://api.rplay.live", "123", "tok")
    assert result.ok is False
    assert "超时" in result.title


def test_check_credentials_network_error_does_not_leak_token(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("failed with token supersecret123")

    _patch_post(monkeypatch, boom)
    result = check_credentials("https://api.rplay.live", "123", "supersecret123")
    assert result.ok is False
    assert "supersecret123" not in result.detail
    assert "supersecret123" not in result.hint


def test_check_credentials_sends_expected_request(monkeypatch):
    seen = {}

    def capture(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return _response(200, {"accessToken": "jwt"})

    _patch_post(monkeypatch, capture)
    check_credentials("https://api.rplay.live/", "123", "refresh")
    assert seen["url"] == "https://api.rplay.live/rplay/account/refresh-token"
    assert seen["headers"]["refresh-token"] == "refresh"
    assert seen["headers"]["platform-type"] == "rplay"
    assert seen["body"] == {"requestorOid": "123"}


# ---------- logs ----------


def test_read_logs_returns_newest_last(tmp_path):
    (tmp_path / "app.log").write_text(
        "2026-03-06 12:00:00 | Main | INFO  | first\n"
        "2026-03-06 12:00:01 | Main | INFO  | second\n",
        encoding="utf-8",
    )
    entries = read_logs(tmp_path)
    assert [entry.message for entry in entries] == [
        "2026-03-06 12:00:00 | Main | INFO  | first",
        "2026-03-06 12:00:01 | Main | INFO  | second",
    ]


def test_read_logs_filters_by_level(tmp_path):
    (tmp_path / "app.log").write_text(
        "2026-03-06 12:00:00 | Main | DEBUG | noise\n"
        "2026-03-06 12:00:01 | Main | ERROR | boom\n",
        encoding="utf-8",
    )
    entries = read_logs(tmp_path, min_level="ERROR")
    assert len(entries) == 1
    assert entries[0].level == "ERROR"


def test_read_logs_respects_limit(tmp_path):
    (tmp_path / "app.log").write_text(
        "\n".join(f"2026-03-06 12:00:0{i % 10} | Main | INFO | line {i}" for i in range(50)),
        encoding="utf-8",
    )
    assert len(read_logs(tmp_path, limit=10)) == 10


def test_read_logs_missing_dir(tmp_path):
    assert read_logs(tmp_path / "nope") == []


def test_read_logs_picks_newest_file(tmp_path):
    (tmp_path / "app.log").write_text(
        "2026-03-06 12:00:00 | Main | INFO | older\n", encoding="utf-8"
    )
    newer = tmp_path / "newer.log"
    newer.write_text("2026-03-06 13:00:00 | Main | INFO | newer\n", encoding="utf-8")
    now = time.time()
    os.utime(tmp_path / "app.log", (now - 9999, now - 9999))
    os.utime(newer, (now, now))
    entries = read_logs(tmp_path)
    assert "newer" in entries[-1].message
