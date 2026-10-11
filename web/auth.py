"""HTTP Basic auth for the web UI.

The UI can read and write the project `.env`, which holds the RPlay
REFRESH_TOKEN. That is harmless on loopback but must not be reachable from the
LAN without credentials, so binding to 0.0.0.0 requires WEB_USERNAME and
WEB_PASSWORD. With both unset the UI stays open, which is the localhost default.
"""

from __future__ import annotations

import hmac
import os
import secrets
from base64 import b64decode
from typing import Optional, Tuple

from fastapi import Request
from fastapi.responses import Response

__all__ = ["auth_config", "verify_basic_auth", "BasicAuthMiddleware", "PUBLIC_PATHS"]

# The Docker healthcheck polls this without credentials.
PUBLIC_PATHS = frozenset({"/healthz"})

_UNAUTHORIZED = (
    b"Authentication required. Set WEB_USERNAME and WEB_PASSWORD for the web service."
)


def auth_config() -> Tuple[Optional[str], Optional[str]]:
    return os.environ.get("WEB_USERNAME") or None, os.environ.get("WEB_PASSWORD") or None


def _parse_basic_auth(header: str) -> Optional[Tuple[str, str]]:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return username, password


def verify_basic_auth(request: Request) -> Optional[Response]:
    """Return a 401 response when access is denied, or None to allow it."""
    username, password = auth_config()
    if not username or not password:
        # Auth not configured: the service is expected to be loopback-only.
        return None

    if request.url.path in PUBLIC_PATHS:
        return None

    credentials = _parse_basic_auth(request.headers.get("authorization", ""))
    if credentials is not None:
        given_user, given_password = credentials
        # compare_digest on both fields keeps the check constant-time.
        user_ok = hmac.compare_digest(given_user, username)
        password_ok = hmac.compare_digest(given_password, password)
        if user_ok and password_ok:
            return None

    return Response(
        content=_UNAUTHORIZED,
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="rplay-live-dl", charset="UTF-8"'},
    )


class BasicAuthMiddleware:
    """Starlette middleware wrapper around verify_basic_auth."""

    def __init__(self, app) -> None:  # noqa: ANN001 - Starlette passes the ASGI app
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        denied = verify_basic_auth(request)
        if denied is not None:
            await denied(scope, receive, send)
            return
        await self.app(scope, receive, send)


def generate_password(length: int = 24) -> str:
    """A URL- and shell-safe password for WEB_PASSWORD."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
