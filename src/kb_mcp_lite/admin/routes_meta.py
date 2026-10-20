"""Meta routes for the admin UI — overview, links, graph, imports, settings, vault."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from kb_mcp_lite.admin._helpers import (
    json_error,
    list_links,
    open_store,
    overview_payload,
    schema_version,
    serialize_link,
)
from kb_mcp_lite.admin import ApiLinkWrite
from kb_mcp_lite.admin._helpers import serialize_doc as _serialize_doc
from kb_mcp_lite.schema import ValidationError, NotFoundError, DuplicateError, IntegrityError
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.vault import (
    VaultAlreadyExistsError,
    VaultManager,
    VaultNotFoundError,
)


def register_meta_routes(app: FastAPI, render: Any) -> None:
    """Register meta/admin routes on the FastAPI app."""

    # ── Health / Stats / Audit ─────────────────────────────────────────

    @app.get("/api/stats")
    def api_stats() -> JSONResponse:
        with open_store(app) as store:
            payload = overview_payload(store)
            return JSONResponse(
                {
                    "stats": payload["stats"],
                    "type_counts": [
                        {"type": type_name, "count": count}
                        for type_name, count in payload["type_counts"]
                    ],
                    "tag_counts": [
                        {"tag": tag, "count": count} for tag, count in payload["tag_counts"]
                    ],
                    "recent_docs": [_serialize_doc(doc) for doc in payload["recent_docs"]],
                    "doctor_report": {
                        "ok": payload["doctor_report"].ok,
                        "checks": [
                            check.model_dump(mode="json")
                            for check in payload["doctor_report"].checks
                        ],
                    },
                    "embed_enabled": payload["embed_enabled"],
                    "embed_dim": payload["embed_dim"],
                }
            )

    @app.get("/api/health")
    def api_health() -> JSONResponse:
        with open_store(app) as store:
            doctor_report = store.doctor()
            return JSONResponse(
                {
                    "ok": doctor_report.ok,
                    "checks": [check.model_dump(mode="json") for check in doctor_report.checks],
                    "db_path": str(store.path),
                    "schema_version": schema_version(store),
                    "audit_log": store.audit_log(limit=50),
                }
            )

    @app.post("/api/doctor/fix")
    def api_doctor_fix() -> JSONResponse:
        with open_store(app) as store:
            from kb_mcp_lite.graph_query import doctor_fix_hygiene
            res = doctor_fix_hygiene(store)
            doctor_report = store.doctor()
            return JSONResponse(
                {
                    "ok": doctor_report.ok,
                    "checks": [check.model_dump(mode="json") for check in doctor_report.checks],
                    "fix_result": res,
                }
            )

    @app.get("/api/audit")
    def api_audit(limit: int = 100) -> JSONResponse:
        with open_store(app) as store:
            return JSONResponse({"items": store.audit_log(limit=limit), "count": limit})

    @app.get("/api/links")
    def api_links() -> JSONResponse:
        with open_store(app) as store:
            links = list_links(store)
            return JSONResponse(
                {
                    "items": [serialize_link(link) for link in links],
                    "count": len(links),
                }
            )

    @app.post("/api/links")
    def api_link_create(payload: ApiLinkWrite) -> JSONResponse:
        with open_store(app) as store:
            try:
                link = store.link(
                    payload.from_id.strip(),
                    payload.to_id.strip(),
                    rel=payload.rel.strip() or "relates-to",
                )
            except NotFoundError as exc:
                return json_error(str(exc), status_code=404)
            except (ValidationError, DuplicateError) as exc:
                return json_error(str(exc), status_code=400)
            except IntegrityError as exc:
                return json_error(str(exc), status_code=500)
            return JSONResponse({"ok": True, "link": serialize_link(link)}, status_code=201)

    @app.delete("/api/links")
    def api_link_delete(payload: ApiLinkWrite) -> JSONResponse:
        with open_store(app) as store:
            removed = store.unlink(
                payload.from_id.strip(),
                payload.to_id.strip(),
                rel=payload.rel.strip() or None,
            )
            return JSONResponse({"ok": True, "removed": removed})

    # ── Graph ──────────────────────────────────────────────────────────

    @app.get("/api/graph")
    def api_graph(root_id: str | None = None, depth: int = 2) -> JSONResponse:
        with open_store(app) as store:
            type_colors = {
                "project": "#1d9bf0",
                "decision": "#00ba7c",
                "lesson": "#f4212e",
                "glossary": "#ffd400",
                "person": "#7856ff",
                "faq": "#0891b2",
            }
            default_color = "#536471"
            if root_id:
                sub = store.subgraph(root_id, depth=depth)
                doc_ids = sub["doc_ids"]
                if doc_ids:
                    ph = ",".join("?" for _ in doc_ids)
                    doc_rows = store._conn.execute(
                        f"SELECT id, title, type FROM documents WHERE id IN ({ph}) AND deleted_at IS NULL",
                        doc_ids,
                    ).fetchall()
                else:
                    doc_rows = []
                nodes = [
                    {
                        "id": r["id"],
                        "label": r["title"],
                        "type": r["type"],
                        "color": type_colors.get(r["type"], default_color),
                        "url": f"/documents/{r['id']}",
                    }
                    for r in doc_rows
                ]
                edges = sub["edges"]
            else:
                active_docs = store.export_all(include_deleted=False)
                links = list_links(store)
                nodes = [
                    {
                        "id": doc.id,
                        "label": doc.title,
                        "type": doc.type,
                        "color": type_colors.get(doc.type, default_color),
                        "url": f"/documents/{doc.id}",
                    }
                    for doc in active_docs
                ]
                edges = [
                    {"from": link.from_id, "to": link.to_id, "label": link.rel} for link in links
                ]
            return JSONResponse({"nodes": nodes, "edges": edges})

    @app.get("/graph", response_class=HTMLResponse)
    def graph_page(request: Request) -> HTMLResponse:
        return render(request, "graph.html")

    # ── Overview ───────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> Any:
        with open_store(app) as store:
            payload = overview_payload(store)
            return render(
                request,
                "overview.html",
                {
                    "stats": payload["stats"],
                    "type_counts": payload["type_counts"],
                    "tag_counts": payload["tag_counts"],
                    "recent_docs": payload["recent_docs"],
                    "doctor_report": payload["doctor_report"],
                    "embed_enabled": payload["embed_enabled"],
                    "embed_dim": payload["embed_dim"],
                    "db_path": str(store.path),
                },
            )

    # ── Links HTML pages ───────────────────────────────────────────────

    @app.get("/links", response_class=HTMLResponse)
    def links_page(
        request: Request,
        doc_id: str = "",
        rel: str = "",
        page: int = 1,
        per_page: int = 20,
    ) -> HTMLResponse:
        per_page = max(5, min(per_page, 100))
        page = max(1, page)
        with open_store(app) as store:
            docs = store.list(limit=500)
            links = list_links(store)
            if doc_id:
                links = [link for link in links if link.from_id == doc_id or link.to_id == doc_id]
            if rel:
                links = [link for link in links if link.rel == rel]
            rel_options = sorted({link.rel for link in list_links(store)})

            total = len(links)
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, total_pages)
            start = (page - 1) * per_page
            end = start + per_page
            paginated_links = links[start:end]

            # Map doc_id to title for more friendly display
            id_to_title = {doc.id: doc.title for doc in docs}

            return render(
                request,
                "links.html",
                {
                    "links": paginated_links,
                    "docs": docs,
                    "id_to_title": id_to_title,
                    "rel_options": rel_options,
                    "filters": {"doc_id": doc_id, "rel": rel},
                    "pagination": {
                        "page": page,
                        "per_page": per_page,
                        "total": total,
                        "total_pages": total_pages,
                        "has_prev": page > 1,
                        "has_next": page < total_pages,
                        "prev_page": page - 1,
                        "next_page": page + 1,
                    },
                },
            )

    @app.post("/links", response_class=HTMLResponse)
    async def links_create(
        from_id: str = Form(...),
        to_id: str = Form(...),
        rel: str = Form(default="relates-to"),
    ) -> RedirectResponse:
        with open_store(app) as store:
            store.link(from_id.strip(), to_id.strip(), rel=rel.strip() or "relates-to")
        return RedirectResponse(url="/links?created=1", status_code=303)

    @app.post("/links/delete", response_class=HTMLResponse)
    async def links_delete(
        from_id: str = Form(...),
        to_id: str = Form(...),
        rel: str = Form(default=""),
    ) -> RedirectResponse:
        with open_store(app) as store:
            store.unlink(from_id.strip(), to_id.strip(), rel=rel.strip() or None)
        return RedirectResponse(url="/links?deleted=1", status_code=303)

    # ── Import / Export ─────────────────────────────────────────────────

    from kb_mcp_lite.md_io import export_dir, import_dir

    @app.get("/imports", response_class=HTMLResponse)
    def imports_page(request: Request) -> HTMLResponse:
        return render(
            request,
            "imports.html",
            {
                "import_report": None,
                "export_report": None,
                "errors": [],
            },
        )

    @app.post("/imports", response_class=HTMLResponse)
    async def imports_run(
        request: Request,
        archive: UploadFile = File(...),
        dry_run: bool = Form(default=False),
    ) -> HTMLResponse:
        errors: list[str] = []
        import_report = None
        export_report = None
        try:
            with open_store(app) as store:
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    payload = await archive.read()
                    zip_path = tmp_path / (archive.filename or "import.zip")
                    zip_path.write_bytes(payload)
                    import zipfile

                    with zipfile.ZipFile(zip_path) as zf:
                        zf.extractall(tmp_path / "vault")
                    import_report = import_dir(store, tmp_path / "vault", dry_run=dry_run)
        except (ValidationError, DuplicateError) as exc:
            errors.append(str(exc))
        except (NotFoundError, IntegrityError) as exc:
            errors.append(str(exc))
        return render(
            request,
            "imports.html",
            {
                "import_report": import_report,
                "export_report": export_report,
                "errors": errors,
            },
            status_code=400 if errors else 200,
        )

    @app.post("/exports", response_class=HTMLResponse)
    def exports_run(request: Request) -> HTMLResponse:
        errors: list[str] = []
        import_report = None
        export_report = None
        try:
            with open_store(app) as store:
                export_home = Path(tempfile.mkdtemp(prefix="kb-mcp-export-"))
                written = export_dir(store, export_home, force=True)
                files = sorted(
                    str(path.relative_to(export_home)) for path in export_home.rglob("*.md")
                )
                export_report = {
                    "path": str(export_home),
                    "written": written,
                    "files": files[:20],
                }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as exc:
            errors.append(str(exc))
        return render(
            request,
            "imports.html",
            {
                "import_report": import_report,
                "export_report": export_report,
                "errors": errors,
            },
            status_code=400 if errors else 200,
        )

    @app.post("/api/imports/upload")
    async def api_imports_upload(
        archive: UploadFile = File(...),
        dry_run: bool = Form(default=False),
    ) -> JSONResponse:
        try:
            with open_store(app) as store:
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    payload = await archive.read()
                    zip_path = tmp_path / (archive.filename or "import.zip")
                    zip_path.write_bytes(payload)
                    import zipfile
                    from kb_mcp_lite.md_io import import_dir
                    with zipfile.ZipFile(zip_path) as zf:
                        zf.extractall(tmp_path / "vault")
                    report = import_dir(store, tmp_path / "vault", dry_run=dry_run)
                    return JSONResponse({"ok": True, "report": report})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.get("/api/exports/download")
    def api_exports_download() -> Any:
        try:
            with open_store(app) as store:
                from kb_mcp_lite.md_io import export_dir
                import zipfile
                import io
                from fastapi.responses import Response

                tmp_export = Path(tempfile.mkdtemp(prefix="kb-export-"))
                export_dir(store, tmp_export, force=True)
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in tmp_export.rglob("*.md"):
                        zf.write(f, arcname=str(f.relative_to(tmp_export)))
                buf.seek(0)
                return Response(
                    content=buf.getvalue(),
                    media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="kb-vault-export.zip"'},
                )
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    # ── Settings ───────────────────────────────────────────────────────

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        with open_store(app) as store:
            doctor_report = store.doctor()
            embedder = getattr(store, "_embedder", None)
            from kb_mcp_lite.config import config_path

            cfg_path = config_path()
            cfg_content = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
            queue_status = store.embedding_queue_status()
            return render(
                request,
                "settings.html",
                {
                    "doctor_report": doctor_report,
                    "db_path": str(store.path),
                    "embedder_enabled": bool(embedder and getattr(embedder, "enabled", False)),
                    "embedder_dim": getattr(embedder, "dim", 0) if embedder else 0,
                    "embed_queue": queue_status.get("counts", {}),
                    "kb_home": os.environ.get("KB_MCP_HOME", ""),
                    "version": schema_version(store),
                    "audit_log": store.audit_log(limit=50),
                    "config_path": str(cfg_path),
                    "config_content": cfg_content,
                },
            )

    # ── Embed Queue ────────────────────────────────────────────────────

    @app.get("/api/embed/status")
    def api_embed_status() -> JSONResponse:
        with open_store(app) as store:
            queue_status = store.embedding_queue_status()
            counts = queue_status.get("counts", {})
            return JSONResponse(
                {
                    "ok": True,
                    "pending": counts.get("pending", 0),
                    "in_progress": counts.get("in_progress", 0),
                    "done": counts.get("done", 0),
                    "failed": counts.get("failed", 0),
                    "counts": counts,
                    "oldest_pending": queue_status.get("oldest_pending"),
                    "oldest_failed": queue_status.get("oldest_failed"),
                }
            )

    @app.post("/api/embed/retry")
    def api_embed_retry(payload: dict[str, Any] | None = None) -> JSONResponse:
        with open_store(app) as store:
            doc_id = payload.get("doc_id") if payload else None
            try:
                retried = store.retry_embedding(doc_id=doc_id)
                return JSONResponse({"ok": True, "retried": retried})
            except Exception as e:
                return json_error(str(e), status_code=500)


    # ── Vault management ───────────────────────────────────────────────

    @app.get("/api/vaults")
    def api_vaults() -> JSONResponse:
        mgr = VaultManager()
        vaults = mgr.list_vaults()
        current = mgr.get_current()
        return JSONResponse(
            {
                "current": current,
                "vaults": [
                    {
                        "name": v.name,
                        "description": v.description,
                        "sync_dir": v.sync_dir,
                    }
                    for v in vaults
                ],
            }
        )

    @app.post("/api/vaults/switch")
    def api_vault_switch(payload: dict[str, str]) -> JSONResponse:
        name = payload.get("name", "")
        if not name:
            return json_error("vault name required", status_code=400)
        mgr = VaultManager()
        try:
            mgr.switch(name)
            new_path = str(mgr.resolve_path(name))
            app.state.store_path = new_path
            return JSONResponse({"ok": True, "current": name, "store_path": new_path})
        except VaultNotFoundError as e:
            return json_error(str(e), status_code=404)
        except VaultAlreadyExistsError as e:
            return json_error(str(e), status_code=409)

    @app.post("/api/vaults/import")
    def api_vault_import() -> JSONResponse:
        mgr = VaultManager()
        name = mgr.get_current()
        try:
            from kb_mcp_lite.md_io import import_dir as _import_dir

            sync_root = mgr._sync_dir(name)
            mdir = mgr.md_dir(name)
            import_target = sync_root if sync_root != mdir else mdir
            if not import_target.exists():
                return JSONResponse(
                    {"ok": False, "error": f"import target {import_target} does not exist"}
                )
            store = SqliteStore(mgr.resolve_path(name))
            try:
                report = _import_dir(store, import_target)
            finally:
                store.close()
            return JSONResponse(
                {
                    "ok": True,
                    "inserted": report.inserted,
                    "updated": report.updated,
                    "skipped": report.skipped,
                    "errors": report.errors[:10],
                }
            )
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            return json_error(str(e), status_code=500)

    @app.post("/api/vaults/commit")
    def api_vault_commit(payload: dict[str, str]) -> JSONResponse:
        message = payload.get("message", "admin commit")
        mgr = VaultManager()
        name = mgr.get_current()
        try:
            output = mgr.commit(message=message, name=name)
            return JSONResponse({"ok": True, "output": output})
        except (VaultNotFoundError, VaultAlreadyExistsError) as e:
            return json_error(str(e), status_code=500)

    @app.post("/api/vaults/embed")
    def api_vault_embed() -> JSONResponse:
        mgr = VaultManager()
        name = mgr.get_current()
        store = SqliteStore(mgr.resolve_path(name))
        try:
            n = store.reindex_embeddings()
            report = getattr(store, "last_reindex_report", {}) or {}
            return JSONResponse(
                {
                    "ok": True,
                    "reindexed": n,
                    "failed": report.get("failed", 0),
                    "dim": report.get("dim", 0),
                    "total": report.get("total", 0),
                }
            )
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            return json_error(str(e), status_code=500)
        finally:
            store.close()

    # ── Config ─────────────────────────────────────────────────────────

    @app.get("/api/config")
    def api_config_get() -> JSONResponse:
        from kb_mcp_lite.config import config_path

        p = config_path()
        if not p.exists():
            return JSONResponse({"ok": False, "error": "config file not found"}, status_code=404)
        return JSONResponse({"ok": True, "path": str(p), "content": p.read_text(encoding="utf-8")})

    @app.put("/api/config")
    def api_config_put(payload: dict[str, str]) -> JSONResponse:
        content = payload.get("content", "")
        if not content:
            return json_error("content is required", status_code=400)
        from kb_mcp_lite.config import config_path

        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return JSONResponse({"ok": True, "path": str(p)})

    # ── Git Management ────────────────────────────────────────────────

    @app.get("/git", response_class=HTMLResponse)
    def page_git(request: Request) -> HTMLResponse:
        return render(request, "git.html", {"active_page": "git"})

    @app.get("/api/git/status")
    def api_git_status() -> JSONResponse:
        mgr = VaultManager()
        try:
            status_data = mgr.git_status_info()
            return JSONResponse({"ok": True, **status_data})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.get("/api/git/history")
    def api_git_history(limit: int = 30) -> JSONResponse:
        mgr = VaultManager()
        try:
            commits = mgr.git_log(limit=limit)
            return JSONResponse({"ok": True, "commits": commits, "count": len(commits)})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.get("/api/git/diff")
    def api_git_diff(
        path: str | None = None,
        staged: bool = False,
        doc_id: str | None = None,
    ) -> JSONResponse:
        mgr = VaultManager()
        try:
            diff_text = mgr.git_diff(path=path, staged=staged)
            pending_diffs = mgr.pending_export_diff(doc_id=doc_id)
            return JSONResponse({
                "ok": True,
                "diff": diff_text,
                "pending_diffs": pending_diffs,
            })
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


    @app.post("/api/git/commit")
    def api_git_commit(payload: dict[str, Any]) -> JSONResponse:
        message = payload.get("message", "admin commit").strip() if isinstance(payload, dict) else "admin commit"
        if not message:
            message = "admin commit"
        full = bool(payload.get("full", False)) if isinstance(payload, dict) else False
        mgr = VaultManager()
        try:
            output = mgr.commit(message=message, full=full)
            return JSONResponse({"ok": True, "output": output})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/api/git/pull")
    def api_git_pull(payload: dict[str, Any] | None = None) -> JSONResponse:
        payload = payload or {}
        remote = payload.get("remote", "origin")
        branch = payload.get("branch", "main")
        mgr = VaultManager()
        try:
            output = mgr.pull(remote=remote, branch=branch)
            return JSONResponse({"ok": True, "output": output})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/api/git/push")
    def api_git_push(payload: dict[str, Any] | None = None) -> JSONResponse:
        payload = payload or {}
        remote = payload.get("remote", "origin")
        branch = payload.get("branch", "main")
        mgr = VaultManager()
        try:
            output = mgr.push(remote=remote, branch=branch)
            return JSONResponse({"ok": True, "output": output})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/api/git/sync")
    def api_git_sync(payload: dict[str, Any] | None = None) -> JSONResponse:
        payload = payload or {}
        message = payload.get("message", "sync: admin auto-commit")
        remote = payload.get("remote", "origin")
        branch = payload.get("branch", "main")
        mgr = VaultManager()
        try:
            output = mgr.sync(message=message, remote=remote, branch=branch)
            return JSONResponse({"ok": True, "output": output})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/api/git/init")
    def api_git_init(payload: dict[str, Any] | None = None) -> JSONResponse:
        payload = payload or {}
        sync_dir = payload.get("sync_dir")
        mgr = VaultManager()
        try:
            output = mgr.init_git(sync_dir=sync_dir)
            return JSONResponse({"ok": True, "output": output})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    # ── Scheduler ──────────────────────────────────────────────────────

    @app.get("/scheduler", response_class=HTMLResponse)
    def page_scheduler(request: Request) -> HTMLResponse:
        return render(request, "scheduler.html", {"active_page": "scheduler"})

    @app.get("/api/scheduler/status")
    def api_scheduler_status() -> JSONResponse:
        with open_store(app) as store:
            from kb_mcp_lite.config import load_config
            from kb_mcp_lite.scheduler import TaskScheduler

            cfg = load_config()
            sched = TaskScheduler(store, cfg)
            return JSONResponse(sched.get_status())

    @app.get("/api/scheduler/tasks")
    def api_scheduler_tasks() -> JSONResponse:
        with open_store(app) as store:
            from kb_mcp_lite.config import load_config
            from kb_mcp_lite.scheduler import TaskScheduler

            cfg = load_config()
            sched = TaskScheduler(store, cfg)
            tasks = sched.list_tasks()
            return JSONResponse({"ok": True, "tasks": tasks, "count": len(tasks)})

    @app.get("/api/scheduler/history")
    def api_scheduler_history(limit: int = 50) -> JSONResponse:
        with open_store(app) as store:
            from kb_mcp_lite.config import load_config
            from kb_mcp_lite.scheduler import TaskScheduler

            cfg = load_config()
            sched = TaskScheduler(store, cfg)
            history = sched.get_history(limit=limit)
            return JSONResponse({"ok": True, "history": history, "count": len(history)})

    @app.post("/api/scheduler/run")
    def api_scheduler_run(payload: dict[str, Any]) -> JSONResponse:
        task_name = payload.get("task_name")
        if not task_name:
            return json_error("task_name is required", status_code=400)
        with open_store(app) as store:
            from kb_mcp_lite.config import load_config
            from kb_mcp_lite.scheduler import TaskScheduler

            cfg = load_config()
            sched = TaskScheduler(store, cfg)
            try:
                run = sched.run_task_now(task_name)
                return JSONResponse(
                    {
                        "ok": run.status == "ok",
                        "task_name": run.task_name,
                        "status": run.status,
                        "duration_ms": run.duration_ms,
                        "error": run.error,
                    }
                )
            except Exception as e:
                return json_error(str(e), status_code=500)

    @app.post("/api/scheduler/tasks/{task_name}/enable")
    def api_scheduler_enable_task(task_name: str) -> JSONResponse:
        with open_store(app) as store:
            from kb_mcp_lite.config import load_config
            from kb_mcp_lite.scheduler import TaskScheduler

            cfg = load_config()
            sched = TaskScheduler(store, cfg)
            try:
                sched.enable_task(task_name)
                return JSONResponse({"ok": True, "task_name": task_name, "enabled": True})
            except Exception as e:
                return json_error(str(e), status_code=500)

    @app.post("/api/scheduler/tasks/{task_name}/disable")
    def api_scheduler_disable_task(task_name: str) -> JSONResponse:
        with open_store(app) as store:
            from kb_mcp_lite.config import load_config
            from kb_mcp_lite.scheduler import TaskScheduler

            cfg = load_config()
            sched = TaskScheduler(store, cfg)
            try:
                sched.disable_task(task_name)
                return JSONResponse({"ok": True, "task_name": task_name, "enabled": False})
            except Exception as e:
                return json_error(str(e), status_code=500)

    @app.put("/api/scheduler/tasks/{task_name}")
    def api_scheduler_update_task(task_name: str, payload: dict[str, Any]) -> JSONResponse:
        with open_store(app) as store:
            from kb_mcp_lite.config import load_config
            from kb_mcp_lite.scheduler import TaskScheduler

            cfg = load_config()
            sched = TaskScheduler(store, cfg)
            try:
                sched.update_task(task_name, payload)
                return JSONResponse({"ok": True, "task_name": task_name, "updated": payload})
            except Exception as e:
                return json_error(str(e), status_code=500)

    # ── Document Types Management ──────────────────────────────────────

    @app.get("/types", response_class=HTMLResponse)
    def types_page(request: Request) -> HTMLResponse:
        with open_store(app) as store:
            from kb_mcp_lite.admin._helpers import BUILTIN_TYPES, get_all_types, get_custom_types

            all_types = get_all_types(store)
            custom_types = get_custom_types()
            total_docs = sum(t.get("doc_count", 0) for t in all_types)
            return render(
                request,
                "types.html",
                {
                    "types": all_types,
                    "stats": {
                        "total": len(all_types),
                        "builtin": len(BUILTIN_TYPES),
                        "custom": len(custom_types),
                        "total_docs": total_docs,
                    },
                },
            )

    @app.get("/api/types")
    def api_types_list() -> JSONResponse:
        with open_store(app) as store:
            from kb_mcp_lite.admin._helpers import BUILTIN_TYPES, get_all_types, get_custom_types

            all_types = get_all_types(store)
            custom_types = get_custom_types()
            total_docs = sum(t.get("doc_count", 0) for t in all_types)
            return JSONResponse(
                {
                    "ok": True,
                    "types": all_types,
                    "stats": {
                        "total": len(all_types),
                        "builtin": len(BUILTIN_TYPES),
                        "custom": len(custom_types),
                        "total_docs": total_docs,
                    },
                }
            )

    @app.post("/api/types")
    def api_types_create(payload: dict[str, Any]) -> JSONResponse:
        raw_name = str(payload.get("name", "")).strip().lower()
        label = str(payload.get("label", "")).strip() or raw_name
        description = str(payload.get("description", "")).strip()
        color = str(payload.get("color", "")).strip() or "#64748b"

        if not raw_name:
            return json_error("类型标识符 (name) 不能为空", status_code=400)
        import re

        if not re.match(r"^[a-z0-9_\-]+$", raw_name):
            return json_error("类型标识符只能包含小写字母、数字、短横线和下划线", status_code=400)

        from kb_mcp_lite.admin._helpers import BUILTIN_TYPES, get_custom_types, save_custom_types

        builtin_names = {t["name"] for t in BUILTIN_TYPES}
        if raw_name in builtin_names:
            return json_error(f"'{raw_name}' 为系统内置类型，无需重复创建", status_code=409)

        custom_types = get_custom_types()
        if any(t.get("name") == raw_name for t in custom_types):
            return json_error(f"自定义类型 '{raw_name}' 已存在", status_code=409)

        new_entry = {
            "name": raw_name,
            "label": label,
            "description": description,
            "color": color,
        }
        custom_types.append(new_entry)
        save_custom_types(custom_types)

        return JSONResponse({"ok": True, "type": new_entry}, status_code=201)

    @app.put("/api/types/{type_name}")
    def api_types_update(type_name: str, payload: dict[str, Any]) -> JSONResponse:
        type_name = type_name.strip().lower()
        if not type_name:
            return json_error("类型标识符不能为空", status_code=400)
        import re

        if not re.match(r"^[a-z0-9_\-]+$", type_name):
            return json_error("类型标识符只能包含小写字母、数字、短横线和下划线", status_code=400)

        label = str(payload.get("label", "")).strip() or type_name
        description = str(payload.get("description", "")).strip()
        color = str(payload.get("color", "")).strip() or "#64748b"

        from kb_mcp_lite.admin._helpers import get_custom_types, save_custom_types

        custom_types = get_custom_types()
        found = False
        updated_item: dict[str, Any] = {}

        for t in custom_types:
            if t.get("name") == type_name:
                t["label"] = label
                t["description"] = description
                t["color"] = color
                found = True
                updated_item = t
                break

        if not found:
            override = {
                "name": type_name,
                "label": label,
                "description": description,
                "color": color,
            }
            custom_types.append(override)
            updated_item = override

        save_custom_types(custom_types)
        return JSONResponse({"ok": True, "type": updated_item})

    @app.delete("/api/types/{type_name}")
    def api_types_delete(type_name: str) -> JSONResponse:
        type_name = type_name.strip().lower()
        from kb_mcp_lite.admin._helpers import BUILTIN_TYPES, get_custom_types, save_custom_types

        builtin_names = {t["name"] for t in BUILTIN_TYPES}
        if type_name in builtin_names:
            return json_error(f"内置类型 '{type_name}' 不可删除", status_code=400)

        with open_store(app) as store:
            doc_cnt = int(
                store._conn.execute(
                    "SELECT COUNT(*) FROM documents WHERE type = ? AND deleted_at IS NULL",
                    (type_name,),
                ).fetchone()[0]
            )
            if doc_cnt > 0:
                return json_error(
                    f"类型 '{type_name}' 下仍有 {doc_cnt} 篇有效文档正在使用，不可删除",
                    status_code=400,
                )

        custom_types = get_custom_types()
        new_list = [t for t in custom_types if t.get("name") != type_name]
        if len(new_list) == len(custom_types):
            return json_error(f"自定义类型 '{type_name}' 不存在", status_code=404)

        save_custom_types(new_list)
        return JSONResponse({"ok": True, "deleted": type_name})


