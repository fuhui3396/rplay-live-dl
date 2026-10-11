"""Validate RPlay credentials with a real refresh-token request.

Runs in a worker thread so the HTTP call never blocks the event loop. Only the
success/failure verdict and a redacted reason are returned; the token itself is
never logged or echoed back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Tuple

import requests

__all__ = ["CredentialCheck", "check_credentials"]

REQUEST_TIMEOUT = 15


@dataclass(frozen=True)
class CredentialCheck:
    ok: bool
    title: str
    detail: str

    @property
    def hint(self) -> str:
        return "" if self.ok else "请重新获取 REFRESH_TOKEN 与 USER_OID 后再试"


def check_credentials(
    base_url: str,
    user_oid: str,
    refresh_token: str,
) -> CredentialCheck:
    """Exchange REFRESH_TOKEN for an access JWT exactly like the recorder does."""
    if not user_oid.strip():
        return CredentialCheck(False, "缺少 USER_OID", "请填写 RPlay 用户 ID")
    if not refresh_token.strip():
        return CredentialCheck(False, "缺少 REFRESH_TOKEN", "请先获取 Refresh Token")

    url = f"{base_url.rstrip('/')}/rplay/account/refresh-token"
    headers = {
        "refresh-token": refresh_token.strip(),
        "platform-type": "rplay",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json={"requestorOid": user_oid.strip()},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        return CredentialCheck(False, "请求超时", "无法连接到 RPlay 服务器")
    except requests.exceptions.RequestException as exc:
        # Never interpolate the exception: it can embed the token in the URL/headers.
        return CredentialCheck(
            False, "网络错误", f"无法连接 RPlay 服务器（{type(exc).__name__}）"
        )

    if response.status_code in (401, 403):
        return CredentialCheck(
            False, "凭证被拒绝", f"服务器返回 HTTP {response.status_code}"
        )
    if response.status_code >= 500:
        return CredentialCheck(
            False, "服务器错误", f"服务器返回 HTTP {response.status_code}，请稍后再试"
        )
    if response.status_code >= 400:
        return CredentialCheck(
            False, "请求被拒绝", f"服务器返回 HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError):
        return CredentialCheck(False, "响应格式异常", "服务器返回了无法解析的内容")

    if not isinstance(data, dict) or not data.get("accessToken"):
        return CredentialCheck(False, "响应中没有 accessToken", "凭证可能已失效")

    return CredentialCheck(True, "凭证有效", "成功获取 access token")
