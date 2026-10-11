"""Tests for web.auth: Basic auth gate for LAN-exposed deployments."""

import base64

import pytest


def _header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def auth_client(host_root, monkeypatch):
    from fastapi.testclient import TestClient

    from web import app as app_module
    from web import paths

    monkeypatch.setattr(paths, "HOST_ROOT", host_root)
    monkeypatch.setattr(app_module.paths, "HOST_ROOT", host_root)
    monkeypatch.setenv("WEB_USERNAME", "admin")
    monkeypatch.setenv("WEB_PASSWORD", "s3cret")
    return TestClient(app_module.create_app())


@pytest.fixture
def open_client(web_client, monkeypatch):
    monkeypatch.delenv("WEB_USERNAME", raising=False)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    return web_client


# ---------- auth disabled ----------


@pytest.mark.parametrize("url", ["/", "/setup", "/recordings", "/logs"])
def test_open_when_auth_not_configured(open_client, url):
    assert open_client.get(url).status_code == 200


# ---------- auth enabled ----------


@pytest.mark.parametrize("url", ["/", "/setup", "/recordings", "/logs", "/api/status"])
def test_unauthenticated_is_rejected(auth_client, url):
    response = auth_client.get(url)
    assert response.status_code == 401
    assert "Basic" in response.headers["www-authenticate"]


def test_correct_credentials_are_accepted(auth_client):
    response = auth_client.get("/", headers=_header("admin", "s3cret"))
    assert response.status_code == 200


def test_static_assets_require_auth_then_serve(auth_client):
    assert auth_client.get("/static/style.css").status_code == 401
    assert (
        auth_client.get("/static/style.css", headers=_header("admin", "s3cret")).status_code
        == 200
    )


@pytest.mark.parametrize(
    "user,password",
    [("admin", "wrong"), ("wrong", "s3cret"), ("", ""), ("admin", "")],
)
def test_wrong_credentials_rejected(auth_client, user, password):
    assert auth_client.get("/", headers=_header(user, password)).status_code == 401


def test_healthz_stays_public_for_docker_probe(auth_client):
    assert auth_client.get("/healthz").status_code == 200


def test_media_requires_auth(auth_client, host_root):
    creator_dir = host_root / "archive" / "Creator"
    creator_dir.mkdir(parents=True, exist_ok=True)
    (creator_dir / "clip.mp4").write_bytes(b"data")
    assert auth_client.get("/media/Creator/clip.mp4").status_code == 401
    assert (
        auth_client.get(
            "/media/Creator/clip.mp4", headers=_header("admin", "s3cret")
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer token",
        "Basic not-base64!!",
        "Basic " + base64.b64encode(b"nocolon").decode(),
        "Basic " + base64.b64encode(b"\xff\xfe:bad").decode(),
    ],
)
def test_malformed_auth_headers_rejected(auth_client, header):
    assert auth_client.get("/", headers={"Authorization": header}).status_code == 401


def test_partial_config_fails_open(auth_client, monkeypatch):
    """Only a username (or only a password) must not half-enable auth."""
    monkeypatch.delenv("WEB_PASSWORD")
    assert auth_client.get("/").status_code == 200


def test_generate_password_is_unique_and_shell_safe():
    from web.auth import generate_password

    values = {generate_password() for _ in range(50)}
    assert len(values) == 50
    for value in values:
        assert len(value) == 24
        assert value.isalnum()
