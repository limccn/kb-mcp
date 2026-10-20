"""Upload staging for the kb admin ingest pipeline (C1).

The staging layer is the boundary where browser-supplied bytes stop being
untrusted and become a clean, enumerable, size-capped tree that the task
engine (C2) can hand to the converter. One :class:`StagingStore` owns every
open batch of the admin process; a batch is a
``tempfile.mkdtemp(prefix="kb-admin-ingest-")`` tree:

    <batch root>/files/        uploaded originals (.zip stays whole)
    <batch root>/out/          reserved for the conversion output (C2)
    <batch root>/_batch.json   manifest: rel/size/source/state/error per file

This module does no conversion, no zip expansion and no vault work, and it
imports no HTTP layer -- ``routes_ingest.py`` (S2) is a thin translation of
these calls and exceptions onto FastAPI. The semantics are fixed by the
task design (D-C1-1..5); three of them shape the code directly:

- **Streaming** (D-C1-2). :meth:`StagingStore.add_file` only consumes the
  caller's ``chunks`` iterator (the caller picks the size, ~1 MiB); no code
  here ever holds a whole file. When a cap trips, the read stops there -- a
  rejected row's ``size`` and its error name the byte count at the break,
  not the file's true size; nothing is read to the end just to print a
  number.
- **Two rejection tiers** (D-C1-1). A file over ``max_file_bytes`` becomes
  a ``rejected`` manifest row and the batch continues (the user can DELETE
  the row); an upload that would cross ``max_files`` / ``max_total_bytes``
  is refused *without* entering the manifest and raises
  :class:`BatchLimitError`, which names the limit and the offending value.
- **Untrusted rel** (D-7). The client-chosen relative path is checked
  against the named rejection classes first (they pin *why*), then against
  the resolved containment of ``files/`` -- the actual filesystem relation
  is the final word, and nothing is ever written outside ``files/``.

Memory is authoritative: ``Batch.files`` (insertion-ordered) is the live
manifest and every mutation rewrites ``_batch.json`` atomically (temp file
+ ``os.replace``). A single process-wide lock serialises all mutations --
this is a local single-user admin; per-batch locking can wait. The store
registers one ``atexit`` handler on its first batch, and every batch
directory it created is removed when the process exits (D-8).
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kb_mcp_lite.docconvert import SUFFIXES

__all__ = [
    "BATCH_PREFIX",
    "Limits",
    "FileRow",
    "Batch",
    "StagingStore",
    "IngestStagingError",
    "BatchNotFoundError",
    "InvalidRelError",
    "ConflictingRelError",
    "UnknownFileError",
    "BatchLimitError",
    "EmptyBatchError",
    "STAGED",
    "REJECTED",
]

#: Manifest row states. A ``staged`` row has a real file under ``files/``;
#: a ``rejected`` row is virtual -- it records a refused upload so the page
#: can name the reason and offer DELETE, and it owns no file on disk.
STAGED = "staged"
REJECTED = "rejected"

#: Temp-dir prefix for one batch. Kept distinct from the pre-existing
#: ``kb-ingest-`` prefix so existing e2e isolation checks don't flag live
#: staging trees as leftovers (design D-8).
BATCH_PREFIX = "kb-admin-ingest-"

_MANIFEST_NAME = "_batch.json"

#: What the batch converter actually picks up: the frozen
#: ``kb_mcp_lite.docconvert.SUFFIXES`` set plus the container suffix it
#: expands -- never a second suffix table. The alignment test pins this
#: decision to the converter's own classification, sample by sample, so
#: the two cannot drift. ``.doc`` stays out on purpose: the batch
#: converter records it as a named skip, so a ``.doc``-only batch must be
#: refused here rather than handed to a converter that exits 2 on
#: "nothing to convert".
_CONVERTIBLE_SUFFIXES = frozenset(SUFFIXES) | {".zip"}


@dataclass(frozen=True)
class Limits:
    """Upload caps for one batch (design §2.4).

    ``max_file_bytes`` is checked against each upload's running total and
    blames that one file; the other two are checked against the batch and
    refuse the upload entirely. Comparisons are strict: a value equal to
    the limit passes, limit + 1 trips.
    """

    max_file_bytes: int = 52_428_800     # 50 MiB per upload
    max_files: int = 200                 # staged rows per batch
    max_total_bytes: int = 524_288_000   # 500 MiB staged per batch


@dataclass(frozen=True)
class FileRow:
    """One manifest row. ``error`` is set exactly for ``rejected`` rows."""

    rel: str
    size: int
    source: str                 # "upload" | "folder"
    state: str                  # STAGED | REJECTED
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "rel": self.rel,
            "size": self.size,
            "source": self.source,
            "state": self.state,
            "error": self.error,
        }


@dataclass
class Batch:
    """One open batch: its directory, its vault, and its manifest rows."""

    batch_id: str
    vault: str
    root: Path
    created_at: str
    files: dict[str, FileRow] = field(default_factory=dict)

    @property
    def files_root(self) -> Path:
        return self.root / "files"

    @property
    def out_root(self) -> Path:
        return self.root / "out"

    def staged_rows(self) -> list[FileRow]:
        return [row for row in self.files.values() if row.state == STAGED]

    def totals(self) -> dict:
        """Counts for the API responses: staged rows only (§2.4)."""
        staged = self.staged_rows()
        return {"count": len(staged), "bytes": sum(row.size for row in staged)}

    def to_dict(self) -> dict:
        """The ``_batch.json`` shape (parent design §3)."""
        return {
            "batch_id": self.batch_id,
            "vault": self.vault,
            "created_at": self.created_at,
            "files": [row.to_dict() for row in self.files.values()],
            "totals": self.totals(),
        }


class IngestStagingError(Exception):
    """Base for staging-domain refusals; the routes map these onto 4xx."""


class BatchNotFoundError(IngestStagingError):
    """No batch with that id is open in this process."""


class InvalidRelError(IngestStagingError):
    """The client-supplied rel is not a safe path inside ``files/``."""


class ConflictingRelError(InvalidRelError):
    """The rel case-insensitively matches a staged row under another
    spelling. On Windows both names fold to one physical file, so the
    upload is refused by name instead of silently sharing it; the routes'
    existing ``InvalidRelError`` -> 400 mapping covers this subclass."""


class UnknownFileError(IngestStagingError):
    """The batch exists but holds no manifest row for that rel."""


class BatchLimitError(IngestStagingError):
    """One upload refused because a batch-wide limit would be crossed.

    The upload does not enter the manifest (D-C1-1); the attributes carry
    exactly what the 409 response must name: which limit, its configured
    value, and the actual value that crossed it.
    """

    def __init__(self, limit: str, limit_value: int, actual: int) -> None:
        super().__init__(f"{limit} limit exceeded (limit={limit_value}, actual={actual})")
        self.limit = limit
        self.limit_value = limit_value
        self.actual = actual


class EmptyBatchError(IngestStagingError):
    """``start`` refused: the batch has nothing the converter would take."""


def _validate_rel(files_root: Path, rel: str) -> None:
    """Refuse any rel that does not name a path inside ``files_root``.

    The named rules are the fast layer -- they pin *why* an input is bad
    (design §2.2) -- and nothing is normalised on the way in: a backslash
    stays a backslash and is refused by name rather than turned into a
    separator. The final word is the resolved containment of the actual
    filesystem path, so the fast layer only has to be exhaustive for the
    shapes the tests draw.
    """
    stripped = rel.strip()
    if not stripped or stripped == ".":
        raise InvalidRelError(f"invalid rel {rel!r}: empty rel")
    if "\\" in rel:
        raise InvalidRelError(f"invalid rel {rel!r}: backslash in rel")
    if rel.startswith("/"):
        raise InvalidRelError(f"invalid rel {rel!r}: absolute rel")
    if ":" in rel.split("/", 1)[0]:
        raise InvalidRelError(f"invalid rel {rel!r}: drive letter in rel")
    segments = rel.split("/")
    if ".." in segments:
        raise InvalidRelError(f"invalid rel {rel!r}: parent traversal in rel")
    if "" in segments:
        raise InvalidRelError(f"invalid rel {rel!r}: empty path segment")
    if any(segment.endswith((" ", ".")) for segment in segments):
        raise InvalidRelError(
            f"invalid rel {rel!r}: trailing space or dot in rel segment")
    target = (files_root / rel).resolve()
    if not target.is_relative_to(files_root.resolve()):
        raise InvalidRelError(f"invalid rel {rel!r}: escapes staging root")


class StagingStore:
    """Owns the open ingest batches of one admin process."""

    def __init__(self, limits: Limits | None = None) -> None:
        #: Test seam only -- production callers take the defaults; there is
        #: deliberately no env/config override (D-C1-3).
        self.limits = limits or Limits()
        self._lock = threading.Lock()
        self._batches: dict[str, Batch] = {}
        self._atexit_registered = False

    # ── batches ─────────────────────────────────────────────────────────

    def create_batch(self, vault: str) -> Batch:
        """Create one batch: a fresh temp tree plus an empty manifest."""
        root = Path(tempfile.mkdtemp(prefix=BATCH_PREFIX))
        batch = Batch(
            batch_id=uuid.uuid4().hex,
            vault=vault,
            root=root,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        batch.files_root.mkdir()
        batch.out_root.mkdir()
        with self._lock:
            self._batches[batch.batch_id] = batch
            if not self._atexit_registered:
                atexit.register(self._cleanup_all_batches)
                self._atexit_registered = True
            self._persist(batch)
        return batch

    def get_batch(self, batch_id: str) -> Batch:
        with self._lock:
            return self._get_batch_locked(batch_id)

    def _get_batch_locked(self, batch_id: str) -> Batch:
        """Caller holds the lock."""
        batch = self._batches.get(batch_id)
        if batch is None:
            raise BatchNotFoundError(f"unknown batch: {batch_id}")
        return batch

    def cleanup_batch(self, batch_id: str) -> None:
        """Remove a batch's directory and forget it (C2 calls this once the
        job's TTL runs out). Like every batch lookup here, an unknown id
        raises :class:`BatchNotFoundError` -- cleanup stays a single, named
        step rather than half a silent no-op."""
        with self._lock:
            batch = self._get_batch_locked(batch_id)
            del self._batches[batch_id]
            shutil.rmtree(batch.root, ignore_errors=True)

    def _cleanup_all_batches(self) -> None:
        """``atexit``: remove every batch directory this process created."""
        with self._lock:
            roots = [batch.root for batch in self._batches.values()]
            self._batches.clear()
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)

    # ── files ───────────────────────────────────────────────────────────

    def add_file(
        self,
        batch_id: str,
        rel: str,
        source: str,
        chunks: Iterable[bytes],
    ) -> FileRow:
        """Stage one upload, streaming ``chunks`` onto disk under ``files/``.

        Returns the manifest row -- normally ``staged``; a file that crosses
        ``max_file_bytes`` is aborted mid-stream and returned as a
        ``rejected`` row with the batch continuing. An upload that would
        cross ``max_files`` / ``max_total_bytes`` raises
        :class:`BatchLimitError` and leaves the manifest untouched.

        The bytes are written to ``<target>.part`` and renamed into place
        only after the read survives the limits, so a failed or refused
        upload never leaves a partial target file behind. A rel that was
        staged before is replaced wholesale when it is written again.
        """
        with self._lock:
            batch = self._get_batch_locked(batch_id)
            _validate_rel(batch.files_root, rel)

            staged = batch.staged_rows()
            for staged_row in staged:
                if staged_row.rel != rel and staged_row.rel.casefold() == rel.casefold():
                    raise ConflictingRelError(
                        f"invalid rel {rel!r}: case-variant of staged "
                        f"{staged_row.rel!r} (DELETE the staged row to replace it)")
            staged_bytes = sum(row.size for row in staged)
            if len(staged) >= self.limits.max_files:
                raise BatchLimitError("max_files", self.limits.max_files, len(staged) + 1)

            target = batch.files_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(target.name + ".part")

            received = 0
            file_over = False
            total_over = False
            try:
                with open(part, "wb") as sink:
                    for chunk in chunks:
                        sink.write(chunk)
                        received += len(chunk)
                        if received > self.limits.max_file_bytes:
                            file_over = True
                            break
                        if staged_bytes + received > self.limits.max_total_bytes:
                            total_over = True
                            break
                if not file_over and not total_over:
                    os.replace(part, target)
            except BaseException:
                # Any half-way failure -- a raising iterator, a dead client,
                # a rename that lost to a directory of the same name -- must
                # not leave the .part file behind.
                part.unlink(missing_ok=True)
                raise

            if total_over:
                # Batch-level refusal: the manifest is untouched and the
                # files already staged stay exactly as they were (D-C1-1).
                part.unlink(missing_ok=True)
                raise BatchLimitError(
                    "max_total_bytes", self.limits.max_total_bytes,
                    staged_bytes + received)

            if file_over:
                # Blame this one file, keep the batch going. ``received`` is
                # the count at the break, which is all the proof of excess
                # the message needs (D-C1-2).
                part.unlink(missing_ok=True)
                # A previous upload of this rel is being replaced by a
                # refused one; its bytes must leave with its row.
                target.unlink(missing_ok=True)
                row = FileRow(
                    rel=rel,
                    size=received,
                    source=source,
                    state=REJECTED,
                    error=(f"exceeds single-file limit {self.limits.max_file_bytes} "
                           f"bytes (aborted after {received} bytes read)"),
                )
                batch.files[rel] = row
                self._persist(batch)
                return row

            row = FileRow(rel=rel, size=received, source=source, state=STAGED)
            batch.files[rel] = row
            self._persist(batch)
            return row

    def remove_file(self, batch_id: str, rel: str) -> FileRow:
        """Drop one manifest row and return it.

        A ``staged`` row's bytes leave disk with it; a ``rejected`` row is
        virtual, so deleting it only deletes the row -- the user's escape
        hatch for a refused upload.
        """
        with self._lock:
            batch = self._get_batch_locked(batch_id)
            _validate_rel(batch.files_root, rel)
            row = batch.files.get(rel)
            if row is None:
                raise UnknownFileError(f"unknown file in batch {batch.batch_id}: {rel!r}")
            del batch.files[rel]
            if row.state == STAGED:
                (batch.files_root / rel).unlink(missing_ok=True)
            self._persist(batch)
            return row

    # ── start guard ─────────────────────────────────────────────────────

    def ensure_startable(self, batch: Batch) -> None:
        """Refuse ``start`` when the converter would find nothing to do.

        A batch with no staged files, or none with a suffix the converter
        takes, would make the batch converter exit 2 -- a
        ``ConverterRunError`` for C2, not a message for the user. The check
        belongs here, at request time, before any job exists.

        "Convertible" means :data:`_CONVERTIBLE_SUFFIXES`, matched
        case-insensitively exactly like the converter's own classification
        (``suffix.lower()``); the alignment test pins the two decisions to
        each other per sample.
        """
        with self._lock:
            staged = batch.staged_rows()
            if not staged:
                raise EmptyBatchError(
                    f"batch {batch.batch_id} has nothing to convert: 0 staged files")
            if not any(Path(row.rel).suffix.lower() in _CONVERTIBLE_SUFFIXES
                       for row in staged):
                raise EmptyBatchError(
                    f"batch {batch.batch_id} has nothing to convert: none of the "
                    f"{len(staged)} staged files has a convertible suffix")

    # ── manifest ────────────────────────────────────────────────────────

    def _persist(self, batch: Batch) -> None:
        """Atomically (re)write ``_batch.json``: temp file + ``os.replace``,
        so a concurrent reader never sees a half-written manifest."""
        tmp = batch.root / (_MANIFEST_NAME + ".tmp")
        tmp.write_text(
            json.dumps(batch.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, batch.root / _MANIFEST_NAME)