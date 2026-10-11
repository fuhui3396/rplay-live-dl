"""FastAPI application: setup wizard, creator management, recording browser.

Runs as its own container process. It only reads and writes the same files the
recorder uses (config/config.yaml, .env, archive/), so the recorder keeps running
untouched and picks up creator edits on its next poll.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from web import paths
from web.archive import ArchiveStore, parse_range_header
from web.auth import BasicAuthMiddleware
from web.credentials import check_credentials
from web.logs import newest_log_mtime, read_logs
from web.store import ENV_FIELDS, ConfigStore

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

GIB = 1024**3


def get_store() -> ConfigStore:
    return ConfigStore(config_path=paths.config_file(), env_path=paths.env_file())


def get_archive() -> ArchiveStore:
    return ArchiveStore(paths.archive_root())


class Creator(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    id: str = Field(min_length=1)
    api_base_url: str = "https://api.rplay.live"


class SettingsUpdate(BaseModel):
    values: Dict[str, str]


def create_app() -> FastAPI:
    app = FastAPI(title="rplay-live-dl", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    # Added last so it wraps every route, including the mounted static files.
    app.add_middleware(BasicAuthMiddleware)

    # ---------- helpers ----------

    def _setup_state(store: ConfigStore) -> Dict[str, Any]:
        config, config_warnings = store.read_config()
        env_status = store.env_status()
        credentials_ready = (
            env_status["USER_OID"]["set"]
            and not env_status["USER_OID"]["placeholder"]
            and env_status["REFRESH_TOKEN"]["set"]
            and not env_status["REFRESH_TOKEN"]["placeholder"]
        )
        creators = config.get("creators") or []
        return {
            "config": config,
            "config_warnings": config_warnings,
            "env_status": env_status,
            "credentials_ready": credentials_ready,
            "creators": creators,
            "creators_ready": bool(creators),
            "complete": credentials_ready and bool(creators),
        }

    def _base_context(request: Request, store: ConfigStore) -> Dict[str, Any]:
        state = _setup_state(store)
        return {
            "request": request,
            "complete": state["complete"],
            "credentials_ready": state["credentials_ready"],
            "creators_ready": state["creators_ready"],
        }

    # ---------- pages ----------

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request, store: ConfigStore = Depends(get_store)
    ) -> Any:
        state = _setup_state(store)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                **_base_context(request, store),
                "state": state,
                "env_fields": ENV_FIELDS,
                "creators": state["creators"],
            },
        )

    @app.get("/recordings", response_class=HTMLResponse)
    def recordings_page(
        request: Request,
        creator: Optional[str] = None,
        store: ConfigStore = Depends(get_store),
        archive: ArchiveStore = Depends(get_archive),
    ) -> Any:
        items = archive.list_recordings()
        creators = sorted({item.creator for item in items})
        if creator:
            items = [item for item in items if item.creator == creator]
        return templates.TemplateResponse(
            request,
            "recordings.html",
            {
                **_base_context(request, store),
                "recordings": items,
                "creators": creators,
                "selected_creator": creator or "",
                "total_size": archive.total_size(),
            },
        )

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(
        request: Request,
        level: str = "INFO",
        store: ConfigStore = Depends(get_store),
    ) -> Any:
        entries = read_logs(paths.logs_root(), limit=400, min_level=level)
        return templates.TemplateResponse(
            request,
            "logs.html",
            {
                **_base_context(request, store),
                "entries": entries,
                "level": level,
                "log_mtime": newest_log_mtime(paths.logs_root()),
            },
        )

    # ---------- setup wizard ----------

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(
        request: Request, store: ConfigStore = Depends(get_store)
    ) -> Any:
        state = _setup_state(store)
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                **_base_context(request, store),
                "state": state,
                "env_fields": ENV_FIELDS,
                "creators": state["creators"],
                "config_warnings": state["config_warnings"],
                "check": None,
            },
        )

    @app.post("/setup")
    async def setup_submit(
        request: Request,
        user_oid: str = Form(""),
        refresh_token: str = Form(""),
        api_base_url: str = Form("https://api.rplay.live"),
        creator_name: str = Form(""),
        creator_id: str = Form(""),
        verify: str = Form(""),
        store: ConfigStore = Depends(get_store),
    ) -> Any:
        errors: List[str] = []
        saved_any = False
        check = None

        has_user_oid = bool(user_oid.strip())
        has_token = bool(refresh_token.strip())
        has_creator = bool(creator_name.strip() or creator_id.strip())

        # Credentials and creators are independent sections: a user may add a
        # creator before obtaining a token, or fix one without the other. Only
        # complain about a section that was actually submitted.
        wants_credentials = has_user_oid or has_token
        if wants_credentials:
            if not has_user_oid:
                errors.append("请填写 RPlay 用户 ID")
            if not has_token:
                errors.append("请填写 Refresh Token")

        if verify and wants_credentials and not errors:
            check = await asyncio.to_thread(
                check_credentials,
                api_base_url or "https://api.rplay.live",
                user_oid,
                refresh_token,
            )
            if not check.ok:
                # Saving credentials we just proved bad would leave the recorder
                # exiting on auth failure, so refuse and let the user fix them.
                errors.append(f"凭证验证未通过：{check.title} — {check.detail}")

        if wants_credentials and not errors:
            try:
                store.update_env(
                    {"USER_OID": user_oid, "REFRESH_TOKEN": refresh_token}
                )
                saved_any = True
            except ValueError as exc:
                errors.append(str(exc))

        if has_creator:
            if not creator_name.strip() or not creator_id.strip():
                errors.append("添加主播需要同时填写名称和 Creator OID")
            else:
                state = _setup_state(store)
                creators = state["creators"]
                if any(c.get("id") == creator_id.strip() for c in creators):
                    errors.append("该主播 ID 已存在")
                else:
                    try:
                        store.write_config(
                            creators
                            + [{"name": creator_name.strip(), "id": creator_id.strip()}],
                            api_base_url,
                        )
                        saved_any = True
                    except ValueError as exc:
                        errors.append(str(exc))

        if not wants_credentials and not has_creator:
            errors.append("请填写凭证或主播信息")

        state = _setup_state(store)
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                **_base_context(request, store),
                "state": state,
                "env_fields": ENV_FIELDS,
                "creators": state["creators"],
                "config_warnings": state["config_warnings"],
                "check": check,
                "saved": saved_any,
                "errors": errors,
            },
            status_code=400 if errors else 200,
        )

    # ---------- creator API ----------

    @app.post("/api/creators")
    def add_creator(
        payload: Creator, store: ConfigStore = Depends(get_store)
    ) -> Any:
        data, _ = store.read_config()
        creators = data.get("creators") or []
        if any(c.get("id") == payload.id.strip() for c in creators):
            raise HTTPException(status_code=409, detail="该主播 ID 已存在")
        creators = creators + [{"name": payload.name.strip(), "id": payload.id.strip()}]
        try:
            store.write_config(creators, payload.api_base_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "creators": creators})

    @app.post("/api/creators/delete")
    def delete_creator(
        creator_oid: str = Form(...), store: ConfigStore = Depends(get_store)
    ) -> Any:
        try:
            store.delete_creator(creator_oid.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/recordings", status_code=303)

    @app.post("/api/settings")
    def update_settings(
        payload: SettingsUpdate, store: ConfigStore = Depends(get_store)
    ) -> Any:
        try:
            changed = store.update_env(payload.values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "changed": changed})

    @app.post("/api/settings/test")
    def test_credentials(
        payload: SettingsUpdate, store: ConfigStore = Depends(get_store)
    ) -> Any:
        values = store.read_env()
        base_url = payload.values.get("apiBaseUrl") or "https://api.rplay.live"
        check = check_credentials(
            base_url,
            payload.values.get("USER_OID") or values.get("USER_OID", ""),
            payload.values.get("REFRESH_TOKEN") or values.get("REFRESH_TOKEN", ""),
        )
        return JSONResponse(
            {
                "ok": check.ok,
                "title": check.title,
                "detail": check.detail,
                "hint": check.hint,
            }
        )

    @app.get("/api/status")
    def api_status(
        store: ConfigStore = Depends(get_store),
        archive: ArchiveStore = Depends(get_archive),
    ) -> Any:
        state = _setup_state(store)
        recordings = archive.list_recordings()
        free_bytes = 0
        try:
            free_bytes = archive.disk_usage(paths.archive_root())
        except OSError:
            free_bytes = 0
        return {
            "configured": state["complete"],
            "creators": len(state["creators"]),
            "recordings": len([r for r in recordings if not r.is_raw]),
            "raw_files": len([r for r in recordings if r.is_raw]),
            "free_gb": round(free_bytes / GIB, 1),
        }

    @app.get("/api/logs")
    def api_logs(level: str = "INFO") -> Any:
        return {
            "entries": [
                {
                    "timestamp": entry.timestamp,
                    "level": entry.level,
                    "message": entry.message,
                }
                for entry in read_logs(paths.logs_root(), limit=200, min_level=level)
            ]
        }

    # ---------- media ----------

    @app.get("/media/{creator}/{filename}")
    def stream_media(
        creator: str,
        filename: str,
        request: Request,
        archive: ArchiveStore = Depends(get_archive),
    ) -> Any:
        path = paths.resolve_within(paths.archive_root(), creator, filename)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")

        try:
            file_size = path.stat().st_size
        except OSError as exc:
            raise HTTPException(status_code=404, detail="文件不可读") from exc

        media_type = archive.guess_media_type(path.name)
        range_header = request.headers.get("range")

        try:
            byte_range = parse_range_header(range_header or "", file_size)
        except ValueError:
            return Response(
                status_code=416, headers={"Content-Range": f"bytes */{file_size}"}
            )

        if byte_range is None:
            return FileResponse(path, media_type=media_type)

        start, end = byte_range
        length = end - start + 1
        return StreamingResponse(
            archive.iter_file(path, start, length),
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
            },
        )

    @app.get("/download/{creator}/{filename}")
    def download_media(
        creator: str, filename: str, archive: ArchiveStore = Depends(get_archive)
    ) -> Any:
        path = paths.resolve_within(paths.archive_root(), creator, filename)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)

    @app.get("/healthz")
    def healthz() -> Any:
        return {"ok": True}

    return app


app = create_app()
