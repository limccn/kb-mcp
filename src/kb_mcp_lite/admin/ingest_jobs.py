"""Ingest job engine for the kb admin console (C2).

One :class:`JobStore` owns every job of the admin process. A job is one
:func:`kb_mcp_lite.ingest.run_ingest` call over a C1 staging batch, run on a
daemon thread with two seams threaded through it:

* ``report_path=batch.root/"report.jsonl"`` -- the converter's report lives
  inside the batch where the engine can read it;
* ``on_progress`` -- one callback per complete report record (S1's seam), so
  every file's conversion state comes from the JSONL line itself, never
  from a post-hoc summary (prd Req 4).

The per-file picture is built live: a record whose ``rel`` hits a staged row
overwrites that row; a record under a staged ``.zip`` appends a member row
(``rel`` keeps the archive prefix -- that is the traceability contract) and
flips the pack row to ``ok``/``expanded: N members``; anything else is
appended defensively -- a record is never dropped. Upload stage is ``done``
for every row (C1 already streamed the bytes to disk); the import stage is
**not** fake-real-time: it starts as ``pending`` and is backfilled after the
fact at finalize, marked ``posthoc: true`` (D-C2-8).

Hidden files and hidden directories never enter the converter's scan
(measured fact: ``convert_docs`` skips dot-prefixed names), so a hidden row
would otherwise sit ``pending`` forever; :meth:`JobStore.start` pre-marks it
``skipped`` with a named reason, and any later record for it would simply
overwrite that mark (self-healing).

Terminality: ``done`` only means "the pipeline ran to the end" -- per-file
failures are named facts, judged from the report, never from the exit code.
``failed`` is reserved for the pipeline itself raising. TTL: terminal jobs
and their batch directory are reaped lazily (~30 min by default) at the
next ``start`` / ``summaries`` / ``snapshot`` -- the in-memory table never
outlives the process, and the directory side is also covered by C1's
``atexit``.

The store takes its lock for every table and row mutation (C1's pattern; a
local single-user admin does not need per-job locking), and every read API
returns a freshly built dict under that lock, so a polling reader can never
observe a half-written job. Lock order is JobStore -> StagingStore, never
the reverse: the worker never holds the job lock while running the child
process, only while touching rows and state.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kb_mcp_lite.admin.ingest_staging import Batch, BatchNotFoundError, StagingStore
from kb_mcp_lite.ingest import IngestResult, run_ingest

__all__ = [
    "CONVERTING",
    "DEFAULT_TTL_SECONDS",
    "DONE",
    "FAILED",
    "HIDDEN_REASON",
    "IMPORTING",
    "LOG_TAIL_CAP",
    "QUEUED",
    "UNREPORTED_REASON",
    "Job",
    "JobConflictError",
    "JobFileRow",
    "JobNotFoundError",
    "JobStore",
]

#: Job lifecycle states (parent design §3).
QUEUED = "queued"
CONVERTING = "converting"
IMPORTING = "importing"
DONE = "done"
FAILED = "failed"

_TERMINAL_STATES = (DONE, FAILED)

#: ``log_tail`` keeps only the tail; a SKIP-heavy batch would otherwise grow
#: without bound in memory (design §8).
LOG_TAIL_CAP = 500

#: Pre-marked reason for a hidden row: the converter never scans dot-prefixed
#: names, so no report record can arrive for it (D-C2-6).
HIDDEN_REASON = "hidden file (not scanned by the converter)"

#: Finalize mark for a row the converter never reported (unparsable report).
UNREPORTED_REASON = "not reported by the converter"

#: Terminal jobs (and their batch directory) are reaped this long after the
#: newest job of the batch finished (design §5 "TTL 与生命周期").
DEFAULT_TTL_SECONDS = 1800.0

#: Report statuses that feed the backfill maps (a product exists on disk).
_PRODUCT_STATUSES = frozenset({"ok", "unchanged"})


class JobNotFoundError(Exception):
    """No job with that id is tracked (already swept, or never existed)."""


class JobConflictError(Exception):
    """``start`` refused: the batch already has a job that is not terminal.

    :meth:`JobStore.start` is the only raiser; the routes map this onto a
    409 that names the running job.
    """

    def __init__(self, batch_id: str, job_id: str) -> None:
        super().__init__(f"batch {batch_id} already has a running job: {job_id}")
        self.batch_id = batch_id
        self.job_id = job_id


@dataclass
class JobFileRow:
    """One file of a job: its three pipeline stages.

    ``import_`` (the wire key is ``import``) stays ``pending`` for rows
    without a product; successful and failed import outcomes are backfilled
    at finalize and marked ``posthoc: true``.
    """

    rel: str
    upload: dict
    convert: dict
    import_: dict

    def to_dict(self) -> dict:
        return {
            "rel": self.rel,
            "upload": dict(self.upload),
            "convert": dict(self.convert),
            "import": dict(self.import_),
        }


def _pending_import() -> dict:
    return {"state": "pending", "error": None, "posthoc": False}


def _is_hidden(rel: str) -> bool:
    """A rel any segment of which starts with ``.`` (the converter's rule)."""
    return any(segment.startswith(".") for segment in rel.split("/"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Job:
    """One ingest run: its batch, its per-file rows, and its outcome.

    ``started_at``/``finished_at`` are tz-aware UTC datetimes internally and
    ISO strings on the wire. ``out_map``/``out_ids``/``staged_rels`` are
    streaming-time bookkeeping, never serialized.
    """

    job_id: str
    batch_id: str
    vault: str
    state: str = QUEUED
    files: dict[str, JobFileRow] = field(default_factory=dict)
    counts: dict = field(
        default_factory=lambda: {
            "uploaded": 0,
            "converted": 0,
            "failed": 0,
            "skipped": 0,
            "inserted": 0,
            "updated": 0,
            "import_errors": 0,
        }
    )
    log_tail: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    #: out rel (as reported) -> source rel; only ok/unchanged with an ``out``.
    out_map: dict[str, str] = field(default_factory=dict, repr=False)
    #: out rel minus ``.md`` (the import-side file id form) -> source rel.
    out_ids: dict[str, str] = field(default_factory=dict, repr=False)
    #: The staged snapshot's rels; only these can be zip packs.
    staged_rels: set[str] = field(default_factory=set, repr=False)

    def to_dict(self) -> dict:
        """The full ``jobs/{id}`` shape (parent design §3 + C2 additions)."""
        return {
            "job_id": self.job_id,
            "batch_id": self.batch_id,
            "vault": self.vault,
            "state": self.state,
            "files": [row.to_dict() for row in self.files.values()],
            "counts": dict(self.counts),
            "log_tail": list(self.log_tail),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
        }


class JobStore:
    """Owns every ingest job of one admin process."""

    def __init__(
        self,
        staging: StagingStore,
        converter: str | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._staging = staging
        #: ``run_ingest``'s ``converter`` argument verbatim (None = discovery).
        self._converter = converter
        #: Test seam and operational knob (design §5); no env override.
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}  # insertion order == start order

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self, batch: Batch) -> Job:
        """Create the job for ``batch`` and run it on a daemon thread.

        The snapshot (staged rows only -- rejected rows never convert) is
        taken here, under the lock; hidden rows are pre-marked ``skipped``.
        A batch that already has a non-terminal job is refused by name.
        """
        with self._lock:
            self._sweep_locked(exclude_batch_id=batch.batch_id)
            running = self._running_locked(batch.batch_id)
            if running is not None:
                raise JobConflictError(batch.batch_id, running.job_id)
            job = Job(job_id=uuid.uuid4().hex, batch_id=batch.batch_id, vault=batch.vault)
            for row in batch.staged_rows():
                job.staged_rels.add(row.rel)
                job.files[row.rel] = JobFileRow(
                    rel=row.rel,
                    upload={"state": "done"},
                    convert=(
                        {"state": "skipped", "reason": HIDDEN_REASON}
                        if _is_hidden(row.rel)
                        else {"state": "pending", "reason": None}
                    ),
                    import_=_pending_import(),
                )
            job.counts["uploaded"] = len(job.files)
            self._jobs[job.job_id] = job
        threading.Thread(target=self._run, args=(job, batch), daemon=True).start()
        return job

    def running_job(self, batch_id: str) -> str | None:
        """The id of the batch's non-terminal job, or None.

        The routes' guard: uploads and deletes are refused while this is not
        None -- the converter is reading ``files/`` as it runs, and a change
        under it would fork the job from the disk facts.
        """
        with self._lock:
            running = self._running_locked(batch_id)
            return running.job_id if running is not None else None

    def snapshot(self, job_id: str) -> dict:
        """The full job shape, built under the lock (safe to poll)."""
        with self._lock:
            self._sweep_locked()
            return self._get_locked(job_id).to_dict()

    def summaries(self) -> list[dict]:
        """Every tracked job's summary, newest first.

        The summary is the full shape minus ``files``/``log_tail`` (design
        §8): the reopen-the-page list does not need the heavy body.
        """
        with self._lock:
            self._sweep_locked()
            summaries = []
            for job in reversed(list(self._jobs.values())):
                data = job.to_dict()
                del data["files"]
                del data["log_tail"]
                summaries.append(data)
            return summaries

    # ── worker ──────────────────────────────────────────────────────────

    def _run(self, job: Job, batch: Batch) -> None:
        """The daemon thread: flip to converting, run, finalize."""
        with self._lock:
            job.state = CONVERTING
            job.started_at = _now()
        try:
            result = run_ingest(
                batch.files_root,
                batch.out_root,
                vault_name=batch.vault,
                converter=self._converter,
                report_path=batch.root / "report.jsonl",
                on_progress=self._handler(job),
            )
        except Exception as exc:  # noqa: BLE001 -- the pipeline's failure is the job's
            with self._lock:
                job.state = FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = _now()
            return
        with self._lock:
            self._finalize_locked(job, batch, result)

    def _handler(self, job: Job):
        """The ``on_progress`` callback: every record updates rows under the lock."""

        def on_progress(record: dict) -> None:
            with self._lock:
                self._handle_record_locked(job, record)

        return on_progress

    def _handle_record_locked(self, job: Job, record: dict) -> None:
        rel = str(record.get("rel", ""))
        status = record.get("status")
        reason = record.get("error") or None

        row = job.files.get(rel)
        if row is not None:
            row.convert = {"state": status, "reason": reason}
        else:
            # Not a staged row: a zip member (traceable by its prefix) or an
            # orphan -- either way the record is appended, never dropped.
            pack = self._matching_pack_locked(job, rel)
            job.files[rel] = JobFileRow(
                rel=rel,
                upload={"state": "done"},
                convert={"state": status, "reason": reason},
                import_=_pending_import(),
            )
            if pack is not None:
                members = sum(
                    1
                    for other in job.files
                    if other != pack.rel and other.startswith(pack.rel + "/")
                )
                pack.convert = {"state": "ok", "reason": f"expanded: {members} members"}

        if status in _PRODUCT_STATUSES and record.get("out"):
            out_rel = str(record["out"])
            job.out_map[out_rel] = rel
            job.out_ids[Path(out_rel).with_suffix("").as_posix()] = rel

        # Runtime fold of the three visible buckets; finalize overwrites with
        # the authoritative IngestResult counts.
        if status == "ok":
            job.counts["converted"] += 1
        elif status == "failed":
            job.counts["failed"] += 1
        elif status == "skipped":
            job.counts["skipped"] += 1

        # Stage derivation (D-C2-7): every staged row decided -> importing.
        # A process label only; the terminal state is always written by
        # finalize.
        if job.state == CONVERTING and all(
            row.convert.get("state") != "pending" for row in job.files.values()
        ):
            job.state = IMPORTING

    def _matching_pack_locked(self, job: Job, rel: str) -> JobFileRow | None:
        """The staged ``.zip`` row that owns ``rel``, by path prefix (longest
        match wins). Members keep the archive name in their rel, so prefix
        equality is the traceability rule; a non-staged row can never claim
        pack ownership."""
        best: JobFileRow | None = None
        for row in job.files.values():
            if row.rel not in job.staged_rels or not row.rel.lower().endswith(".zip"):
                continue
            if rel.startswith(row.rel + "/") and (
                best is None or len(row.rel) > len(best.rel)
            ):
                best = row
        return best

    def _finalize_locked(self, job: Job, batch: Batch, result: IngestResult) -> None:
        """The worker's single exit: authoritative counts, log tail, backfill."""
        summary = result.import_summary
        job.counts["converted"] = result.converted
        job.counts["failed"] = result.failed
        job.counts["skipped"] = len(result.skipped)
        job.counts["inserted"] = summary.inserted if summary is not None else 0
        job.counts["updated"] = summary.updated if summary is not None else 0
        errors = list(summary.errors) if summary is not None else []
        job.counts["import_errors"] = len(errors)

        # Backfill (D-C2-8): each import error is matched against the
        # products this run reported. A hit marks that row failed; a miss
        # (kb-index trees, link errors, relative-path variants) is never
        # guessed at -- it goes to the log tail verbatim.
        import_lines: list[str] = []
        for error in errors:
            src_rel = self._match_import_error(error, batch.out_root, job)
            if src_rel is not None and src_rel in job.files:
                job.files[src_rel].import_ = {
                    "state": "failed",
                    "error": error,
                    "posthoc": True,
                }
            else:
                import_lines.append(f"[import] {error}")

        # Success rows: a row whose conversion produced a product and that
        # was not hit by an error is imported-after-the-fact too.
        for row in job.files.values():
            if row.import_["state"] != "pending":
                continue
            if row.convert.get("state") in _PRODUCT_STATUSES:
                row.import_ = {"state": "ok", "error": None, "posthoc": True}

        # A row the converter never reported (its report was unreadable) is
        # named, not left hanging.
        for row in job.files.values():
            if row.convert.get("state") == "pending":
                row.convert = {"state": "skipped", "reason": UNREPORTED_REASON}

        job.log_tail = (list(result.notable_lines) + import_lines)[-LOG_TAIL_CAP:]
        job.state = DONE
        job.finished_at = _now()

    @staticmethod
    def _match_import_error(error: str, out_root: Path, job: Job) -> str | None:
        """One import error -> the source rel it belongs to, or None.

        ``md_io``'s two promised forms: (a) a file-level error whose head is
        the product's *absolute* path -- posix-ified relative to the out
        root and matched against the reported products; (b) a
        ``store.import_many`` error whose head is the file-id form (out rel
        minus ``.md``). The head is ``error.split(": ", 1)[0]``.
        """
        head = error.split(": ", 1)[0]
        head_path = Path(head)
        if head_path.is_absolute():
            try:
                rel = head_path.resolve().relative_to(out_root.resolve()).as_posix()
            except ValueError:
                rel = None
            if rel is not None and rel in job.out_map:
                return job.out_map[rel]
        return job.out_ids.get(head_path.as_posix())

    # ── TTL ─────────────────────────────────────────────────────────────

    def _sweep_locked(self, exclude_batch_id: str | None = None) -> None:
        """Reap expired terminal jobs (and their batch directories).

        A batch is due when none of its jobs is non-terminal and the newest
        ``finished_at`` is older than the TTL; a rerun in progress keeps the
        whole batch alive. ``exclude_batch_id`` exempts the batch a ``start``
        is about to run: starting a rerun is the opposite of abandonment,
        and reaping it here would delete the tree under the new job's feet
        (the TTL clock restarts when the new job finishes). Lock order
        JobStore -> StagingStore.
        """
        now = _now()
        by_batch: dict[str, list[Job]] = {}
        for job in self._jobs.values():
            by_batch.setdefault(job.batch_id, []).append(job)
        for batch_id, jobs in by_batch.items():
            if batch_id == exclude_batch_id:
                continue
            if any(job.state not in _TERMINAL_STATES for job in jobs):
                continue
            if any(job.finished_at is None for job in jobs):
                continue
            newest = max(job.finished_at for job in jobs)
            if (now - newest).total_seconds() <= self.ttl_seconds:
                continue
            for job in jobs:
                del self._jobs[job.job_id]
            try:
                self._staging.cleanup_batch(batch_id)
            except BatchNotFoundError:
                pass  # already gone (e.g. C1 atexit raced a test teardown)

    # ── lookup helpers ──────────────────────────────────────────────────

    def _get_locked(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"unknown job: {job_id}")
        return job

    def _running_locked(self, batch_id: str) -> Job | None:
        for job in self._jobs.values():
            if job.batch_id == batch_id and job.state not in _TERMINAL_STATES:
                return job
        return None