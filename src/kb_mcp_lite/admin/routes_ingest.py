"""Ingest staging routes for the admin API (C1's HTTP surface).

A thin translation of :mod:`kb_mcp_lite.admin.ingest_staging` onto
FastAPI: every upload semantic (rel safety, caps, the two rejection
tiers) lives in the domain module, and this file only shapes requests and
maps exceptions.

    GET    /ingest                         the ingest page (C3, SSR shell)
    POST   /api/ingest/batches             create a batch (optional vault)
    POST   /api/ingest/batches/{id}/files  stream one upload into files/
    DELETE /api/ingest/batches/{id}/files/{rel}
    POST   /api/ingest/batches/{id}/start  start the ingest job -> 202
    GET    /api/ingest/jobs                recent job summaries, newest first
    GET    /api/ingest/jobs/{id}           one job, the full form (polling)
    GET    /api/ingest/limits

Streaming (D-C1-2): the multipart body is never read whole -- starlette
spooled it to ``UploadFile.file`` (a ``SpooledTemporaryFile``: 1 MiB in
memory, then disk) and rewound it after parsing; the route hands the
domain a 1 MiB-chunk generator over that spool and
:meth:`StagingStore.add_file` copies it into ``files/`` as it reads. The
Content-Length fast path is deliberately absent (D-C1-6): the in-stream
check is the only verdict.

Rejections (design sections 2.4 / 3) come in two tiers that stay
distinct:

- one file over ``max_file_bytes`` is *not* an exception -- ``add_file``
  returns a ``rejected`` manifest row, the batch continues, and the
  response is 200 ``{ok: false, file, totals}`` so the page can offer a
  DELETE;
- an upload crossing ``max_files`` / ``max_total_bytes`` raises
  ``BatchLimitError`` and maps to 409 naming ``limit`` / ``limit_value``
  / ``actual``; it never enters the manifest (D-C1-1).

The ``file`` object in the responses is the manifest row verbatim
(``FileRow.to_dict()``), so the wire shape and ``_batch.json`` cannot
drift. Rel refusals map to 400, an unknown batch or rel to 404 -- always
the ``{"ok": false, "error": ...}`` shape. A native ``OSError`` from the
filesystem (a Windows reserved device name, say) is deliberately *not*
translated into a 4xx: the original class and the 500 semantics stay
(design section 2.2).

C2 adds the job surface (design sections 8-9): ``start`` maps the job
engine's own refusals (nothing convertible -> 409, a second start -> 409
naming the running job) and answers 202 with the job id; the two job GETs
pass through the engine's summary list and full snapshot verbatim. The two
file-mutating routes carry the job guard: a batch with a non-terminal job
refuses uploads and deletions with a 409 naming the job -- the converter
is reading ``files/`` as it runs, and a change under it would fork the job
from the disk facts. The guard sits after the batch lookup (a 404 keeps
priority) and before the domain call.

C3 adds the page route (``GET /ingest``) to this module so the whole ingest
feature -- page and API -- ships as one registration call; the template is
an SSR shell only, and every job fact on screen is fetched by the page's
own JavaScript from the API routes below it (single authority: the server).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict
from typing import IO, Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from kb_mcp_lite.admin._helpers import json_error
from kb_mcp_lite.admin.ingest_jobs import (
    JobConflictError,
    JobNotFoundError,
    JobStore,
)
from kb_mcp_lite.admin.ingest_staging import (
    STAGED,
    BatchLimitError,
    BatchNotFoundError,
    EmptyBatchError,
    InvalidRelError,
    StagingStore,
    UnknownFileError,
)
from kb_mcp_lite.vault import get_current_vault_name

#: Upload reads move in 1 MiB chunks (design section 2.3); the domain
#: streams them into ``files/`` without ever holding the whole file.
CHUNK_SIZE = 1024 * 1024

_SOURCES = ("upload", "folder")


class BatchCreate(BaseModel):
    """The optional JSON body of ``POST /api/ingest/batches``."""

    vault: str | None = None


def _chunks(spool: IO[bytes]) -> Iterator[bytes]:
    """Yield the spool's bytes in :data:`CHUNK_SIZE` blocks, at most once
    through -- the generator form :meth:`StagingStore.add_file` consumes.
    The spool is starlette's, already rewound by the multipart parser."""
    while True:
        chunk = spool.read(CHUNK_SIZE)
        if not chunk:
            return
        yield chunk


def register_ingest_routes(app: FastAPI, render: Any) -> None:
    """Register the ingest page and staging routes on the FastAPI app."""

    # ── Page (C3) ──────────────────────────────────────────────────────

    @app.get("/ingest", response_class=HTMLResponse)
    def ingest_page(request: Request) -> HTMLResponse:
        """The SSR shell only: controls plus empty containers. All job data
        is fetched by the page's own JS, so SSR and polling never grow two
        renderings of the same facts."""
        return render(request, "ingest.html")

    def _running_job_refusal(batch_id: str) -> JSONResponse | None:
        """A 409 naming the batch's non-terminal job, or None (C2 guard)."""
        jobs: JobStore = app.state.jobs
        running = jobs.running_job(batch_id)
        if running is None:
            return None
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"batch {batch_id} has a running job: {running}; "
                    "file changes are refused until it finishes"
                ),
                "job_id": running,
            },
            status_code=409,
        )

    @app.post("/api/ingest/batches")
    def api_ingest_batch_create(payload: BatchCreate | None = None) -> JSONResponse:
        staging: StagingStore = app.state.staging
        vault = payload.vault if payload else None
        if not vault:
            vault = get_current_vault_name()
        batch = staging.create_batch(vault)
        return JSONResponse(
            {
                "ok": True,
                "batch_id": batch.batch_id,
                "vault": batch.vault,
                "limits": asdict(staging.limits),
                "totals": batch.totals(),
            },
            status_code=201,
        )

    @app.post("/api/ingest/batches/{batch_id}/files")
    async def api_ingest_file_upload(
        batch_id: str,
        file: UploadFile = File(...),
        rel: str = Form(default=""),
        source: str = Form(default="upload"),
    ) -> JSONResponse:
        staging: StagingStore = app.state.staging
        if source not in _SOURCES:
            return json_error(f"invalid source {source!r}: expected 'upload' or 'folder'")
        try:
            staging.get_batch(batch_id)
        except BatchNotFoundError as exc:
            return json_error(str(exc), status_code=404)
        refusal = _running_job_refusal(batch_id)
        if refusal is not None:
            return refusal
        try:
            row = staging.add_file(batch_id, rel, source, _chunks(file.file))
        except BatchNotFoundError as exc:
            return json_error(str(exc), status_code=404)
        except InvalidRelError as exc:
            return json_error(str(exc), status_code=400)
        except BatchLimitError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error": str(exc),
                    "limit": exc.limit,
                    "limit_value": exc.limit_value,
                    "actual": exc.actual,
                },
                status_code=409,
            )
        batch = staging.get_batch(batch_id)
        staged = row.state == STAGED
        return JSONResponse(
            {"ok": staged, "file": row.to_dict(), "totals": batch.totals()},
            status_code=201 if staged else 200,
        )

    @app.delete("/api/ingest/batches/{batch_id}/files/{rel:path}")
    def api_ingest_file_delete(batch_id: str, rel: str) -> JSONResponse:
        staging: StagingStore = app.state.staging
        try:
            staging.get_batch(batch_id)
        except BatchNotFoundError as exc:
            return json_error(str(exc), status_code=404)
        refusal = _running_job_refusal(batch_id)
        if refusal is not None:
            return refusal
        try:
            staging.remove_file(batch_id, rel)
        except BatchNotFoundError as exc:
            return json_error(str(exc), status_code=404)
        except InvalidRelError as exc:
            return json_error(str(exc), status_code=400)
        except UnknownFileError as exc:
            return json_error(str(exc), status_code=404)
        batch = staging.get_batch(batch_id)
        return JSONResponse({"ok": True, "removed": True, "totals": batch.totals()})

    @app.post("/api/ingest/batches/{batch_id}/start")
    def api_ingest_batch_start(batch_id: str) -> JSONResponse:
        staging: StagingStore = app.state.staging
        jobs: JobStore = app.state.jobs
        try:
            batch = staging.get_batch(batch_id)
        except BatchNotFoundError as exc:
            return json_error(str(exc), status_code=404)
        try:
            staging.ensure_startable(batch)
        except EmptyBatchError as exc:
            return json_error(str(exc), status_code=409)
        try:
            job = jobs.start(batch)  # also runs the lazy TTL sweep
        except JobConflictError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "job_id": exc.job_id},
                status_code=409,
            )
        return JSONResponse(
            {
                "ok": True,
                "job_id": job.job_id,
                "batch_id": batch.batch_id,
                "state": job.state,
            },
            status_code=202,
        )

    @app.get("/api/ingest/jobs")
    def api_ingest_jobs_list() -> JSONResponse:
        jobs: JobStore = app.state.jobs
        return JSONResponse({"ok": True, "jobs": jobs.summaries()})

    @app.get("/api/ingest/jobs/{job_id}")
    def api_ingest_job_detail(job_id: str) -> JSONResponse:
        jobs: JobStore = app.state.jobs
        try:
            return JSONResponse(jobs.snapshot(job_id))
        except JobNotFoundError as exc:
            return json_error(str(exc), status_code=404)

    @app.get("/api/ingest/limits")
    def api_ingest_limits() -> JSONResponse:
        staging: StagingStore = app.state.staging
        return JSONResponse({"limits": asdict(staging.limits)})
