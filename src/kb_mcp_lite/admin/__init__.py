"""FastAPI admin console for kb-mcp."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from kb_mcp_lite.admin._helpers import (
    SEARCH_MODES,
    create_default_store,
    open_store,
)
from kb_mcp_lite.admin.ingest_staging import Limits, StagingStore
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.vault import get_current_vault_name

_THIS_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = _THIS_DIR.parent  # kb_mcp_lite/
TEMPLATES_DIR = PACKAGE_DIR / "templates" / "admin"
STATIC_DIR = PACKAGE_DIR / "static"
STATIC_APP_DIR = STATIC_DIR / "app"


class ApiDocCreate(BaseModel):
    id: str = ""
    type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    tags: list[str] | None = None
    source: str | None = None
    body: str = ""


class ApiDocUpdate(BaseModel):
    title: str | None = None
    tags: list[str] | None = None
    source: str | None = None
    body: str | None = None
    deleted: bool | None = None


class ApiLinkWrite(BaseModel):
    from_id: str = Field(min_length=1)
    to_id: str = Field(min_length=1)
    rel: str = "relates-to"


def create_app(
    store: SqliteStore | None = None,
    staging_limits: Limits | None = None,
    ingest_converter: str | None = None,
    ingest_ttl_seconds: float | None = None,
) -> FastAPI:
    app = FastAPI(title="kb-mcp admin", version="0.1")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    if (STATIC_APP_DIR / "assets").exists():
        app.mount("/app/assets", StaticFiles(directory=str(STATIC_APP_DIR / "assets")), name="spa_app_assets")
        app.mount("/assets", StaticFiles(directory=str(STATIC_APP_DIR / "assets")), name="spa_assets")

    @app.get("/app/favicon.ico")
    @app.get("/favicon.ico")
    def serve_favicon_ico() -> Any:
        ico_file = STATIC_APP_DIR / "favicon.ico"
        if not ico_file.exists():
            ico_file = STATIC_DIR / "favicon.ico"
        if ico_file.exists():
            return FileResponse(str(ico_file), media_type="image/x-icon")
        return HTMLResponse("", status_code=404)

    @app.get("/app/favicon.svg")
    @app.get("/favicon.svg")
    def serve_favicon_svg() -> Any:
        svg_file = STATIC_APP_DIR / "favicon.svg"
        if not svg_file.exists():
            svg_file = STATIC_DIR / "favicon.svg"
        if svg_file.exists():
            return FileResponse(str(svg_file), media_type="image/svg+xml")
        return HTMLResponse("", status_code=404)

    @app.get("/app/{full_path:path}")
    @app.get("/app")
    def serve_spa(full_path: str = "") -> Any:
        index_file = STATIC_APP_DIR / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return HTMLResponse(
            "<h1>SPA not built yet. Run 'cd web && npm run build'</h1>", status_code=404
        )

    app.state.store_path = str((store or create_default_store()).path)

    # C1: upload staging for the ingest pipeline. ``staging_limits`` is a
    # test seam only (design D-C1-3); production callers take the defaults.
    app.state.staging = StagingStore(limits=staging_limits)

    # C2: the ingest job engine, one per process. ``ingest_converter`` /
    # ``ingest_ttl_seconds`` are test seams only (C1's pattern); production
    # callers take the defaults (converter discovery, 30 min TTL). The
    # function-level import follows the routes-registration pattern below,
    # which keeps the module statically visible to the frozen build.
    from kb_mcp_lite.admin.ingest_jobs import DEFAULT_TTL_SECONDS, JobStore

    app.state.jobs = JobStore(
        app.state.staging,
        converter=ingest_converter,
        ttl_seconds=(
            ingest_ttl_seconds if ingest_ttl_seconds is not None else DEFAULT_TTL_SECONDS
        ),
    )

    def render(
        request: Any,
        template: str,
        context: dict[str, Any] | None = None,
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        try:
            with open_store(app) as st:
                from kb_mcp_lite.admin._helpers import get_all_types

                current_doc_types = get_all_types(st)
        except Exception:
            from kb_mcp_lite.admin._helpers import BUILTIN_TYPES

            current_doc_types = BUILTIN_TYPES

        doc_types_map = {
            t["name"]: t
            for t in current_doc_types
            if isinstance(t, dict) and "name" in t
        }

        payload = {
            "request": request,
            "doc_types": current_doc_types,
            "doc_types_map": doc_types_map,
            "search_modes": SEARCH_MODES,
            "vault_name": get_current_vault_name(),
            "flash": {
                "kind": request.query_params.get("flash", ""),
                "message": request.query_params.get("message", ""),
            },
        }
        if context:
            payload.update(context)
        return templates.TemplateResponse(
            request=request,
            name=template,
            context=payload,
            status_code=status_code,
        )

    from kb_mcp_lite.admin.routes_docs import register_doc_routes
    from kb_mcp_lite.admin.routes_ingest import register_ingest_routes
    from kb_mcp_lite.admin.routes_meta import register_meta_routes

    register_doc_routes(app, render)
    register_meta_routes(app, render)
    register_ingest_routes(app, render)

    return app


def run_admin(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    store: SqliteStore | None = None,
) -> None:
    import uvicorn

    app = create_app(store=store)
    uvicorn.run(app, host=host, port=port, log_level="info")
