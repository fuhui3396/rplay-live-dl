"""Tests for the web HTTP surface: pages, JSON API, and media streaming."""

import json

import pytest


def _make_recording(host_root, name="clip.mp4", data=b"v" * 500):
    creator_dir = host_root / "archive" / "Creator"
    creator_dir.mkdir(parents=True, exist_ok=True)
    path = creator_dir / name
    path.write_bytes(data)
    return path


# ---------- pages ----------


@pytest.mark.parametrize("url", ["/", "/setup", "/recordings", "/logs"])
def test_pages_render_on_empty_installation(web_client, url):
    response = web_client.get(url)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_index_shows_setup_banner_when_unconfigured(web_client):
    body = web_client.get("/").text
    assert "还没有完成初始设置" in body


def test_index_banner_disappears_once_configured(web_client, store):
    store.update_env({"USER_OID": "12345", "REFRESH_TOKEN": "real-token"})
    store.write_config([{"name": "Alice", "id": "x1"}], "https://api.rplay.live")
    body = web_client.get("/").text
    assert "还没有完成初始设置" not in body
    assert "Alice" in body


def test_static_css_is_served(web_client):
    assert web_client.get("/static/style.css").status_code == 200


# ---------- setup wizard ----------


def test_setup_saves_credentials_without_verification(web_client, store):
    response = web_client.post(
        "/setup",
        data={"user_oid": "999", "refresh_token": "tok", "verify": ""},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert store.read_env()["USER_OID"] == "999"
    assert store.read_env()["REFRESH_TOKEN"] == "tok"


def test_setup_requires_both_credentials(web_client, store):
    response = web_client.post(
        "/setup", data={"user_oid": "999", "refresh_token": ""}, follow_redirects=True
    )
    assert response.status_code == 400
    assert "请填写 Refresh Token" in response.text
    assert not store.env_path.exists()


def test_setup_adds_creator_without_credentials(web_client, store):
    """Credentials and creators are independent sections."""
    response = web_client.post(
        "/setup",
        data={"creator_name": "Alice", "creator_id": "oid-alice"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    data, _ = store.read_config()
    assert data["creators"] == [{"name": "Alice", "id": "oid-alice"}]
    assert not store.env_path.exists()


def test_setup_rejects_empty_submission(web_client):
    response = web_client.post("/setup", data={}, follow_redirects=True)
    assert response.status_code == 400
    assert "请填写凭证或主播信息" in response.text


def test_setup_requires_both_creator_fields(web_client, store):
    response = web_client.post(
        "/setup", data={"creator_name": "Alice"}, follow_redirects=True
    )
    assert response.status_code == 400
    assert "同时填写名称和 Creator OID" in response.text
    assert not store.config_path.exists()


def test_setup_reports_duplicate_creator(web_client, store):
    store.write_config([{"name": "Alice", "id": "oid-alice"}], "https://api.rplay.live")
    response = web_client.post(
        "/setup",
        data={"creator_name": "Alice", "creator_id": "oid-alice"},
        follow_redirects=True,
    )
    assert response.status_code == 400
    assert "已存在" in response.text


def test_failed_verification_still_saves_a_creator(web_client, store, monkeypatch):
    from web import app as app_module

    def fake_check(*args, **kwargs):
        from web.credentials import CredentialCheck

        return CredentialCheck(False, "凭证被拒绝", "HTTP 401")

    monkeypatch.setattr(app_module, "check_credentials", fake_check)
    response = web_client.post(
        "/setup",
        data={
            "user_oid": "999",
            "refresh_token": "bad",
            "creator_name": "Alice",
            "creator_id": "oid-alice",
            "verify": "1",
        },
        follow_redirects=True,
    )
    assert response.status_code == 400
    assert not store.env_path.exists()
    # The creator section is independent, so it is still saved and displayed.
    assert store.read_config()[0]["creators"] == [{"name": "Alice", "id": "oid-alice"}]
    assert "Alice" in response.text


def test_setup_adds_creator_and_writes_config(web_client, store):
    web_client.post(
        "/setup",
        data={
            "user_oid": "999",
            "refresh_token": "tok",
            "creator_name": "Alice",
            "creator_id": "oid-alice",
            "api_base_url": "https://api.rplay.live",
        },
        follow_redirects=True,
    )
    data, _ = store.read_config()
    assert data["creators"] == [{"name": "Alice", "id": "oid-alice"}]


def test_setup_does_not_duplicate_existing_creator(web_client, store):
    store.write_config([{"name": "Alice", "id": "oid-alice"}], "https://api.rplay.live")
    web_client.post(
        "/setup",
        data={
            "user_oid": "999",
            "refresh_token": "tok",
            "creator_name": "Alice again",
            "creator_id": "oid-alice",
        },
        follow_redirects=True,
    )
    data, _ = store.read_config()
    assert len(data["creators"]) == 1


def test_setup_verify_reports_failure_without_saving(web_client, store, monkeypatch):
    from web import app as app_module

    def fake_check(*args, **kwargs):
        from web.credentials import CredentialCheck

        return CredentialCheck(False, "凭证被拒绝", "服务器返回 HTTP 401")

    monkeypatch.setattr(app_module, "check_credentials", fake_check)
    response = web_client.post(
        "/setup",
        data={"user_oid": "999", "refresh_token": "bad", "verify": "1"},
        follow_redirects=True,
    )
    assert "凭证被拒绝" in response.text
    assert not store.env_path.exists()


def test_setup_verify_success_saves(web_client, store, monkeypatch):
    from web import app as app_module

    def fake_check(*args, **kwargs):
        from web.credentials import CredentialCheck

        return CredentialCheck(True, "凭证有效", "成功获取 access token")

    monkeypatch.setattr(app_module, "check_credentials", fake_check)
    web_client.post(
        "/setup",
        data={"user_oid": "999", "refresh_token": "good", "verify": "1"},
        follow_redirects=True,
    )
    assert store.read_env()["REFRESH_TOKEN"] == "good"


# ---------- creator API ----------


def test_add_creator_via_api(web_client, store):
    response = web_client.post(
        "/api/creators", json={"name": "Bob", "id": "oid-bob"}
    )
    assert response.status_code == 200
    assert store.read_config()[0]["creators"] == [{"name": "Bob", "id": "oid-bob"}]


def test_add_duplicate_creator_conflicts(web_client, store):
    store.write_config([{"name": "Bob", "id": "oid-bob"}], "https://api.rplay.live")
    response = web_client.post("/api/creators", json={"name": "Bob", "id": "oid-bob"})
    assert response.status_code == 409


def test_add_creator_rejects_blank_name(web_client):
    assert web_client.post("/api/creators", json={"name": "", "id": "x"}).status_code == 422


def test_add_creator_rejects_bad_api_url(web_client):
    response = web_client.post(
        "/api/creators", json={"name": "Bob", "id": "x", "api_base_url": "ftp://x"}
    )
    assert response.status_code == 400


def test_delete_creator_redirects(web_client, store):
    store.write_config(
        [{"name": "A", "id": "x1"}, {"name": "B", "id": "x2"}],
        "https://api.rplay.live",
    )
    response = web_client.post(
        "/api/creators/delete", data={"creator_oid": "x1"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert store.read_config()[0]["creators"] == [{"name": "B", "id": "x2"}]


def test_delete_unknown_creator_returns_400(web_client, store):
    store.write_config([{"name": "A", "id": "x1"}], "https://api.rplay.live")
    response = web_client.post(
        "/api/creators/delete", data={"creator_oid": "nope"}
    )
    assert response.status_code == 400


# ---------- settings API ----------


def test_settings_update_persists(web_client, store):
    response = web_client.post("/api/settings", json={"values": {"INTERVAL": "120"}})
    assert response.status_code == 200
    assert response.json()["changed"] == ["INTERVAL"]
    assert store.read_env()["INTERVAL"] == "120"


def test_settings_update_rejects_invalid_value(web_client):
    response = web_client.post("/api/settings", json={"values": {"INTERVAL": "1"}})
    assert response.status_code == 400


def test_settings_secret_mask_is_ignored(web_client, store):
    store.update_env({"REFRESH_TOKEN": "real"})
    web_client.post("/api/settings", json={"values": {"REFRESH_TOKEN": "********"}})
    assert store.read_env()["REFRESH_TOKEN"] == "real"


def test_settings_test_endpoint_uses_saved_credentials(web_client, store, monkeypatch):
    from web import app as app_module

    captured = {}

    def fake_check(base_url, user_oid, refresh_token):
        captured.update(base_url=base_url, user_oid=user_oid, token=refresh_token)
        from web.credentials import CredentialCheck

        return CredentialCheck(True, "凭证有效", "ok")

    monkeypatch.setattr(app_module, "check_credentials", fake_check)
    store.update_env({"USER_OID": "42", "REFRESH_TOKEN": "tok"})
    response = web_client.post("/api/settings/test", json={"values": {}})
    assert response.json()["ok"] is True
    assert captured["user_oid"] == "42"
    assert captured["token"] == "tok"


# ---------- status / logs API ----------


def test_status_reports_configured_state(web_client, store):
    body = web_client.get("/api/status").json()
    assert body["configured"] is False
    assert body["creators"] == 0

    store.update_env({"USER_OID": "1", "REFRESH_TOKEN": "t"})
    store.write_config([{"name": "A", "id": "x1"}], "https://api.rplay.live")
    body = web_client.get("/api/status").json()
    assert body["configured"] is True
    assert body["creators"] == 1


def test_status_counts_recordings_and_raw_files(web_client, host_root):
    _make_recording(host_root, "clip.mp4")
    _make_recording(host_root, "session.ts")
    body = web_client.get("/api/status").json()
    assert body["recordings"] == 1
    assert body["raw_files"] == 1


def test_logs_endpoint_returns_entries(web_client, host_root):
    (host_root / "logs" / "app.log").write_text(
        "2026-03-06 12:00:00 | Main     | INFO     | hello\n"
        "2026-03-06 12:00:01 | Main     | ERROR    | boom\n",
        encoding="utf-8",
    )
    body = web_client.get("/api/logs?level=ERROR").json()
    assert len(body["entries"]) == 1
    assert "boom" in body["entries"][0]["message"]


def test_logs_endpoint_empty_is_not_an_error(web_client):
    assert web_client.get("/api/logs").json() == {"entries": []}


# ---------- media ----------


def test_media_serves_full_file(web_client, host_root):
    _make_recording(host_root, data=b"abcdefghij")
    response = web_client.get("/media/Creator/clip.mp4")
    assert response.status_code == 200
    assert response.content == b"abcdefghij"
    assert response.headers["content-type"] == "video/mp4"


def test_media_supports_range_requests(web_client, host_root):
    _make_recording(host_root, data=b"0123456789")
    response = web_client.get("/media/Creator/clip.mp4", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"


def test_media_suffix_range(web_client, host_root):
    _make_recording(host_root, data=b"0123456789")
    response = web_client.get("/media/Creator/clip.mp4", headers={"Range": "bytes=-3"})
    assert response.status_code == 206
    assert response.content == b"789"


def test_media_unsatisfiable_range_returns_416(web_client, host_root):
    _make_recording(host_root, data=b"0123456789")
    response = web_client.get("/media/Creator/clip.mp4", headers={"Range": "bytes=999-"})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"


def test_media_missing_file_returns_404(web_client):
    assert web_client.get("/media/Creator/nope.mp4").status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "/media/..%2F..%2Fetc/clip.mp4",
        "/media/Creator/..%2F..%2Fetc%2Fpasswd",
        "/media/Creator/%2Fetc%2Fpasswd",
    ],
)
def test_media_blocks_path_traversal(web_client, host_root, url):
    assert web_client.get(url).status_code in (404, 400)


def test_download_sends_attachment(web_client, host_root):
    _make_recording(host_root, data=b"data")
    response = web_client.get("/download/Creator/clip.mp4")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]


def test_download_blocks_traversal(web_client):
    assert web_client.get("/download/..%2F..%2Fetc/clip.mp4").status_code in (404, 400)


def test_healthz(web_client):
    assert web_client.get("/healthz").json() == {"ok": True}
