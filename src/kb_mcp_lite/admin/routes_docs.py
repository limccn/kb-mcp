"""Document CRUD + search routes for the admin UI."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from kb_mcp_lite.admin._helpers import (
    create_document,
    doc_form_data,
    doc_row,
    filtered_documents,
    flash_url,
    json_error,
    open_store,
    patch_document,
    serialize_doc,
    serialize_hit,
    split_tags,
)
from kb_mcp_lite.admin import ApiDocCreate, ApiDocUpdate
from kb_mcp_lite.admin._helpers import serialize_link
from kb_mcp_lite.relations import STANDARD_RELATIONS
from kb_mcp_lite.schema import (
    SearchHit,
    ValidationError,
    NotFoundError,
    DuplicateError,
    IntegrityError,
)


def register_doc_routes(app: FastAPI, render: Any) -> None:
    """Register document-related routes on the FastAPI app."""

    # ── API routes ─────────────────────────────────────────────────────

    @app.get("/api/docs")
    def api_docs(
        q: str = "",
        type: str = "",
        tag: str = "",
        include_deleted: bool = False,
    ) -> JSONResponse:
        with open_store(app) as store:
            docs = filtered_documents(
                store,
                q=q,
                doc_type=type,
                tag=tag,
                include_deleted=include_deleted,
            )
            return JSONResponse(
                {
                    "items": [serialize_doc(doc) for doc in docs],
                    "count": len(docs),
                }
            )

    @app.get("/api/docs/{doc_id:path}")
    def api_doc_detail(doc_id: str) -> JSONResponse:
        with open_store(app) as store:
            doc = store.get(doc_id, include_deleted=True)
            doc_data = serialize_doc(doc)
            sections = doc.extract_sections()
            return JSONResponse(
                {
                    "doc": doc_data,
                    "sections": sections,
                    "section_headings": [s for s in sections.keys() if s],
                    "outlinks": [serialize_link(link) for link in store.outlinks(doc.id)],
                    "backlinks": [serialize_link(link) for link in store.backlinks(doc.id)],
                    "history": store.document_history(doc.id),
                }
            )

    @app.get("/api/search")
    def api_search(
        query: str,
        type: str = "",
        tags: str = "",
        mode: str = "hybrid",
        limit: int = 10,
    ) -> JSONResponse:
        with open_store(app) as store:
            hits = store.search(
                query=query,
                type=type or None,
                tags=split_tags(tags),
                limit=limit,
                mode=mode,
            )
            return JSONResponse(
                {
                    "hits": [serialize_hit(hit) for hit in hits],
                    "count": len(hits),
                }
            )

    # ── Document CRUD API ──────────────────────────────────────────────

    @app.post("/api/docs")
    def api_doc_create(payload: ApiDocCreate) -> JSONResponse:
        with open_store(app) as store:
            try:
                created = create_document(
                    store,
                    doc_id=payload.id,
                    doc_type=payload.type,
                    title=payload.title,
                    tags=payload.tags,
                    source=payload.source,
                    body=payload.body,
                )
            except (ValidationError, DuplicateError) as exc:
                return json_error(str(exc), status_code=400)
            except (NotFoundError, IntegrityError) as exc:
                return json_error(str(exc), status_code=500)
            return JSONResponse({"ok": True, "doc": serialize_doc(created)}, status_code=201)

    @app.patch("/api/docs/{doc_id:path}")
    def api_doc_update(doc_id: str, payload: ApiDocUpdate) -> JSONResponse:
        with open_store(app) as store:
            try:
                doc = patch_document(
                    store,
                    doc_id,
                    payload.title,
                    payload.tags,
                    payload.source,
                    payload.body,
                    payload.deleted,
                )
            except NotFoundError as exc:
                return json_error(str(exc), status_code=404)
            except ValidationError as exc:
                return json_error(str(exc), status_code=400)
            except IntegrityError as exc:
                return json_error(str(exc), status_code=500)
            return JSONResponse({"ok": True, "doc": serialize_doc(doc)})

    @app.delete("/api/docs/{doc_id:path}")
    def api_doc_delete(doc_id: str) -> JSONResponse:
        with open_store(app) as store:
            try:
                store.delete(doc_id)
                doc = store.get(doc_id, include_deleted=True)
            except NotFoundError as exc:
                return json_error(str(exc), status_code=404)
            except IntegrityError as exc:
                return json_error(str(exc), status_code=500)
            return JSONResponse({"ok": True, "doc": serialize_doc(doc)})

    # ── HTML pages ─────────────────────────────────────────────────────

    @app.get("/documents", response_class=HTMLResponse)
    def documents(
        request: Request,
        q: str = "",
        type: str = "",
        tag: str = "",
        include_deleted: bool = False,
        page: int = 1,
        per_page: int = 20,
    ) -> HTMLResponse:
        per_page = max(5, min(per_page, 100))
        page = max(1, page)
        with open_store(app) as store:
            docs = filtered_documents(
                store,
                q=q,
                doc_type=type,
                tag=tag,
                include_deleted=include_deleted,
            )
            rows = [doc_row(store, doc) for doc in docs]
            total = len(rows)
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, total_pages)
            start = (page - 1) * per_page
            end = start + per_page
            paginated_rows = rows[start:end]
            return render(
                request,
                "documents.html",
                {
                    "rows": paginated_rows,
                    "filters": {
                        "q": q,
                        "type": type,
                        "tag": tag,
                        "include_deleted": include_deleted,
                    },
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

    @app.get("/documents/new", response_class=HTMLResponse)
    def document_new(request: Request) -> HTMLResponse:
        empty = {
            "id": "",
            "type": "project",
            "title": "",
            "tags": "",
            "source": "",
            "body": "",
        }
        return render(
            request,
            "document_form.html",
            {
                "doc_form": empty,
                "editing": False,
                "errors": [],
                "links_out": [],
                "links_back": [],
                "standard_relations": STANDARD_RELATIONS,
            },
        )

    @app.get("/documents/{doc_id:path}", response_class=HTMLResponse)
    def document_detail(request: Request, doc_id: str) -> HTMLResponse:
        with open_store(app) as store:
            try:
                doc = store.get(doc_id, include_deleted=True)
            except NotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except IntegrityError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            return render(
                request,
                "document_form.html",
                {
                    "doc_form": doc_form_data(doc),
                    "editing": True,
                    "errors": [],
                    "links_out": store.outlinks(doc.id),
                    "links_back": store.backlinks(doc.id),
                    "doc": doc,
                    "history": store.document_history(doc.id),
                    "standard_relations": STANDARD_RELATIONS,
                },
            )

    @app.post("/documents/{doc_id:path}/links", response_class=HTMLResponse)
    async def document_link_create(
        request: Request,
        doc_id: str,
        to_id: str = Form(...),
        rel: str = Form(default="relates-to"),
    ) -> Response:
        with open_store(app) as store:
            try:
                store.link(doc_id, to_id.strip(), rel=rel.strip() or "relates-to")
            except NotFoundError as exc:
                doc = store.get(doc_id, include_deleted=True)
                return render(
                    request,
                    "document_form.html",
                    {
                        "doc_form": doc_form_data(doc),
                        "editing": True,
                        "errors": [str(exc)],
                        "links_out": store.outlinks(doc.id),
                        "links_back": store.backlinks(doc.id),
                        "doc": doc,
                        "history": store.document_history(doc.id),
                    },
                    status_code=404,
                )
            except (ValidationError, DuplicateError) as exc:
                doc = store.get(doc_id, include_deleted=True)
                return render(
                    request,
                    "document_form.html",
                    {
                        "doc_form": doc_form_data(doc),
                        "editing": True,
                        "errors": [str(exc)],
                        "links_out": store.outlinks(doc.id),
                        "links_back": store.backlinks(doc.id),
                        "doc": doc,
                        "history": store.document_history(doc.id),
                    },
                    status_code=400,
                )
            except IntegrityError as exc:
                doc = store.get(doc_id, include_deleted=True)
                return render(
                    request,
                    "document_form.html",
                    {
                        "doc_form": doc_form_data(doc),
                        "editing": True,
                        "errors": [str(exc)],
                        "links_out": store.outlinks(doc.id),
                        "links_back": store.backlinks(doc.id),
                        "doc": doc,
                        "history": store.document_history(doc.id),
                    },
                    status_code=500,
                )
        return RedirectResponse(
            url=flash_url(f"/documents/{doc_id}", "success", "Link created"),
            status_code=303,
        )

    @app.post("/documents/{doc_id:path}/links/delete", response_class=HTMLResponse)
    async def document_link_delete(
        doc_id: str,
        to_id: str = Form(...),
        rel: str = Form(default=""),
    ) -> RedirectResponse:
        with open_store(app) as store:
            store.unlink(doc_id, to_id.strip(), rel=rel.strip() or None)
        return RedirectResponse(
            url=flash_url(f"/documents/{doc_id}", "success", "Link removed"),
            status_code=303,
        )

    @app.post("/documents", response_class=HTMLResponse)
    async def document_create(
        request: Request,
        id: str = Form(default=""),
        type: str = Form(...),
        title: str = Form(...),
        tags: str = Form(default=""),
        source: str = Form(default=""),
        body: str = Form(default=""),
    ) -> Response:
        with open_store(app) as store:
            try:
                created = create_document(
                    store,
                    doc_id=id,
                    doc_type=type,
                    title=title,
                    tags=split_tags(tags),
                    source=source,
                    body=body,
                )
                created_id = created.id
            except (ValidationError, DuplicateError) as exc:
                return render(
                    request,
                    "document_form.html",
                    {
                        "doc_form": {
                            "id": id,
                            "type": type,
                            "title": title,
                            "tags": tags,
                            "source": source,
                            "body": body,
                        },
                        "editing": False,
                        "errors": [str(exc)],
                        "links_out": [],
                        "links_back": [],
                        "history": [],
                    },
                    status_code=400,
                )
            except IntegrityError as exc:
                return render(
                    request,
                    "document_form.html",
                    {
                        "doc_form": {
                            "id": id,
                            "type": type,
                            "title": title,
                            "tags": tags,
                            "source": source,
                            "body": body,
                        },
                        "editing": False,
                        "errors": [str(exc)],
                        "links_out": [],
                        "links_back": [],
                        "history": [],
                    },
                    status_code=500,
                )
        return RedirectResponse(
            url=flash_url(f"/documents/{created_id}", "success", "Document created"),
            status_code=303,
        )

    @app.post("/documents/{doc_id:path}", response_class=HTMLResponse)
    async def document_update(
        request: Request,
        doc_id: str,
        title: str = Form(...),
        tags: str = Form(default=""),
        source: str = Form(default=""),
        body: str = Form(default=""),
        action: str = Form(default="save"),
    ) -> Response:
        with open_store(app) as store:
            if action == "delete":
                store.delete(doc_id)
                return RedirectResponse(
                    url=flash_url("/documents", "success", "Document deleted"),
                    status_code=303,
                )
            try:
                patch_document(
                    store,
                    doc_id,
                    title,
                    split_tags(tags),
                    source,
                    body,
                    deleted=None,
                )
            except NotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (ValidationError, DuplicateError) as exc:
                doc = store.get(doc_id, include_deleted=True)
                return render(
                    request,
                    "document_form.html",
                    {
                        "doc_form": {
                            "id": doc.id,
                            "type": doc.type,
                            "title": title,
                            "tags": tags,
                            "source": source,
                            "body": body,
                        },
                        "editing": True,
                        "errors": [str(exc)],
                        "links_out": store.outlinks(doc.id),
                        "links_back": store.backlinks(doc.id),
                        "doc": doc,
                        "history": store.document_history(doc.id),
                    },
                    status_code=400,
                )
            except IntegrityError as exc:
                doc = store.get(doc_id, include_deleted=True)
                return render(
                    request,
                    "document_form.html",
                    {
                        "doc_form": {
                            "id": doc.id,
                            "type": doc.type,
                            "title": title,
                            "tags": tags,
                            "source": source,
                            "body": body,
                        },
                        "editing": True,
                        "errors": [str(exc)],
                        "links_out": store.outlinks(doc.id),
                        "links_back": store.backlinks(doc.id),
                        "doc": doc,
                        "history": store.document_history(doc.id),
                    },
                    status_code=500,
                )
        return RedirectResponse(
            url=flash_url(f"/documents/{doc_id}", "success", "Document updated"),
            status_code=303,
        )

    @app.get("/search", response_class=HTMLResponse)
    def search_lab(
        request: Request,
        query: str = "",
        type: str = "",
        tags: str = "",
        mode: str = "hybrid",
        limit: int = 10,
    ) -> HTMLResponse:
        hits: list[SearchHit] = []
        errors: list[str] = []
        if query.strip():
            with open_store(app) as store:
                try:
                    hits = store.search(
                        query=query,
                        type=type or None,
                        tags=split_tags(tags),
                        limit=limit,
                        mode=mode,
                    )
                except ValidationError as exc:
                    errors.append(str(exc))
        return render(
            request,
            "search.html",
            {
                "hits": hits,
                "errors": errors,
                "filters": {
                    "query": query,
                    "type": type,
                    "tags": tags,
                    "mode": mode,
                    "limit": limit,
                },
            },
        )
