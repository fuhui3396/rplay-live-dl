"""Read and write config/config.yaml and .env on behalf of the web UI.

Both files are shared with the recorder process, so every write is atomic
(write temp file in the same directory, then os.replace). The recorder re-reads
config.yaml on each poll, so creator edits take effect without a restart; .env
changes need a container recreate and the UI says so.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

import yaml
from pydantic import ValidationError

from models.config import CreatorProfile

__all__ = [
    "ENV_FIELDS",
    "SECRET_MASK",
    "ConfigStore",
    "SecretField",
    "atomic_write",
]

_SECRET_MARKERS = ("token", "secret", "password", "webhook")

# Placeholder the UI sends instead of a secret it did not change.
SECRET_MASK = "********"


class SecretField:
    """One editable .env setting, with the metadata the setup wizard needs."""

    def __init__(
        self,
        key: str,
        label: str,
        help_text: str = "",
        secret: bool = False,
        placeholder: str = "",
        group: str = "general",
    ) -> None:
        self.key = key
        self.label = label
        self.help_text = help_text
        self.secret = secret
        self.placeholder = placeholder
        self.group = group


ENV_FIELDS: List[SecretField] = [
    SecretField(
        "USER_OID",
        "RPlay 用户 ID",
        "打开 https://rplay.live/myinfo/ 页面上的 User Number",
        placeholder="例：2000001234567",
        group="credentials",
    ),
    SecretField(
        "REFRESH_TOKEN",
        "Refresh Token",
        "浏览器登录后，F12 控制台运行 README 里的脚本从 IndexedDB 复制",
        secret=True,
        placeholder="粘贴 refreshToken",
        group="credentials",
    ),
    SecretField(
        "INTERVAL",
        "轮询间隔（秒）",
        "10 - 3600，默认 60",
        placeholder="60",
        group="general",
    ),
    SecretField(
        "MIN_FREE_DISK_GB",
        "录制前最小剩余空间（GiB）",
        "默认 5，设为 0 关闭该保护",
        placeholder="5",
        group="general",
    ),
    SecretField(
        "LOG_LEVEL",
        "日志级别",
        "DEBUG / INFO / WARNING / ERROR / CRITICAL",
        placeholder="INFO",
        group="general",
    ),
    SecretField(
        "DISCORD_WEBHOOK_URL",
        "Discord Webhook",
        "留空则关闭通知。填入后需要重建容器",
        secret=True,
        placeholder="https://discord.com/api/webhooks/...",
        group="notifications",
    ),
]

_FIELD_BY_KEY = {field.key: field for field in ENV_FIELDS}

# Matches KEY=VALUE and KEY="VALUE" / KEY='VALUE' on one line.
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def atomic_write(path: Path, content: str) -> None:
    """Replace path with content without ever exposing a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


class ConfigStore:
    """All filesystem access for the web UI, in one place for easy testing."""

    def __init__(
        self,
        config_path: Path,
        env_path: Path,
    ) -> None:
        self.config_path = config_path
        self.env_path = env_path

    # ---------- config/config.yaml ----------

    def read_config(self) -> Tuple[Dict[str, Any], List[str]]:
        """Return (raw mapping, warnings). Invalid creator entries are dropped."""
        warnings: List[str] = []
        if not self.config_path.exists():
            return {}, ["配置文件不存在，保存后会自动创建"]

        try:
            text = self.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            return {}, [f"无法读取配置文件：{exc}"]

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return {}, [f"YAML 格式错误：{exc}"]

        if data is None:
            return {}, []
        if not isinstance(data, dict):
            return {}, ["配置文件顶层必须是字典"]

        creators = data.get("creators")
        if creators is None:
            data["creators"] = []
        elif not isinstance(creators, list):
            warnings.append("creators 不是列表，已重置为空")
            data["creators"] = []
        else:
            data["creators"] = self._sanitize_creators(creators, warnings)

        api_base_url = data.get("apiBaseUrl")
        if api_base_url is not None and not isinstance(api_base_url, str):
            warnings.append("apiBaseUrl 不是字符串，已忽略")
            data.pop("apiBaseUrl")

        return data, warnings

    @staticmethod
    def _sanitize_creators(raw: List[Any], warnings: List[str]) -> List[Dict[str, str]]:
        clean: List[Dict[str, str]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                warnings.append(f"第 {index + 1} 项不是字典，已跳过")
                continue
            try:
                profile = CreatorProfile(
                    creator_name=item.get("name"),
                    creator_oid=item.get("id"),
                )
            except ValidationError:
                warnings.append(f"第 {index + 1} 项缺少 name 或 id，已跳过")
                continue
            clean.append({"name": profile.creator_name, "id": profile.creator_oid})
        return clean

    def write_config(self, creators: List[Dict[str, str]], api_base_url: str) -> None:
        """Validate then atomically replace config.yaml.

        Raises ValueError with a user-facing message when input is invalid, so
        the recorder never sees a config it would choke on.
        """
        validated: List[Dict[str, str]] = []
        for index, item in enumerate(creators):
            try:
                profile = CreatorProfile(
                    creator_name=item.get("name"),
                    creator_oid=item.get("id"),
                )
            except ValidationError as exc:
                raise ValueError(f"第 {index + 1} 个主播数据无效：{exc}") from exc
            validated.append(
                {"name": profile.creator_name, "id": profile.creator_oid}
            )

        if not validated:
            raise ValueError("至少需要保留一个主播")

        try:
            api_base_url = str(api_base_url).strip().rstrip("/")
            parsed = urlparse(api_base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("API 地址无效，请填写以 http:// 或 https:// 开头的地址")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        payload: Dict[str, Any] = {
            "apiBaseUrl": api_base_url,
            "creators": validated,
        }
        body = yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        header = (
            "# 由 Web UI 生成。录制进程每次轮询都会重新读取本文件，\n"
            "# 修改主播列表无需重启容器。\n"
        )
        atomic_write(self.config_path, header + body)

    def delete_creator(self, creator_oid: str) -> None:
        data, _ = self.read_config()
        creators = data.get("creators") or []
        remaining = [c for c in creators if c.get("id") != creator_oid]
        if len(remaining) == len(creators):
            raise ValueError("找不到该主播")
        if not remaining:
            raise ValueError("至少需要保留一个主播")
        self.write_config(remaining, data.get("apiBaseUrl") or "https://api.rplay.live")

    # ---------- .env ----------

    def read_env(self) -> Dict[str, str]:
        """Parse .env into a dict, ignoring comments and blank lines."""
        values: Dict[str, str] = {}
        if not self.env_path.exists():
            return values
        try:
            text = self.env_path.read_text(encoding="utf-8")
        except OSError:
            return values
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = _ENV_LINE_RE.match(line)
            if match:
                values[match.group(1)] = _strip_quotes(match.group(2))
        return values

    def env_status(self) -> Dict[str, Dict[str, bool]]:
        """Per-field state for the wizard: present / looks-plausible."""
        values = self.read_env()
        status: Dict[str, Dict[str, bool]] = {}
        for field in ENV_FIELDS:
            raw = values.get(field.key, "")
            status[field.key] = {
                "set": bool(raw),
                "placeholder": raw.lower()
                in {"your_refresh_token", "your_user_oid", "changeme", ""},
            }
        return status

    def update_env(self, updates: Dict[str, str]) -> List[str]:
        """Update known keys in place, preserving comments and unknown keys.

        A blank value for an optional key clears it. Returns the keys that
        actually changed. The file is rewritten once, so a rejected value
        never leaves a half-applied .env behind.
        """
        known = {field.key for field in ENV_FIELDS}
        pending: Dict[str, str] = {}

        for key, value in updates.items():
            if key not in known:
                continue
            value = value.strip()
            if self._is_secret(key) and value == SECRET_MASK:
                # The UI sends a mask for untouched secrets; keep the real value
                # rather than overwriting it with the mask.
                continue
            if not self._is_valid(key, value):
                raise ValueError(f"{_FIELD_BY_KEY[key].label} 取值不合法")
            if self.read_env().get(key, "") != value:
                pending[key] = value

        if not pending:
            return []

        lines = (
            self.env_path.read_text(encoding="utf-8").splitlines()
            if self.env_path.exists()
            else ["# rplay-live-dl 配置（由 Web UI 生成）"]
        )
        for key, value in pending.items():
            replacement = f"{key}={value}"
            for index, line in enumerate(lines):
                match = _ENV_LINE_RE.match(line)
                if match and match.group(1) == key:
                    lines[index] = replacement
                    break
            else:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.append(replacement)

        atomic_write(self.env_path, "\n".join(lines) + "\n")
        return list(pending)

    @staticmethod
    def _is_secret(key: str) -> bool:
        lowered = key.lower()
        return any(marker in lowered for marker in _SECRET_MARKERS)

    @staticmethod
    def _is_valid(key: str, value: str) -> bool:
        if key in {"USER_OID", "REFRESH_TOKEN"}:
            return bool(value) or key == "REFRESH_TOKEN"
        if key == "INTERVAL":
            return value.isdigit() and 10 <= int(value) <= 3600
        if key == "MIN_FREE_DISK_GB":
            try:
                return float(value) >= 0
            except ValueError:
                return False
        if key == "LOG_LEVEL":
            return value.upper() in {
                "DEBUG",
                "INFO",
                "WARNING",
                "ERROR",
                "CRITICAL",
            }
        return True

    def _write_env_value(self, key: str, value: str) -> None:
        self.update_env({key: value})

    def env_template(self) -> str:
        """A commented .env seeded from .env.example when present."""
        example = self.env_path.parent / ".env.example"
        if example.exists():
            try:
                return example.read_text(encoding="utf-8")
            except OSError:
                pass
        lines = ["# rplay-live-dl 配置（由 Web UI 生成）", ""]
        for field in ENV_FIELDS:
            if field.help_text:
                lines.append(f"# {field.help_text}")
            lines.append(f"{field.key}=")
            lines.append("")
        return "\n".join(lines)
