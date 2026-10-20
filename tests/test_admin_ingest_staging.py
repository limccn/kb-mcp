"""Domain tests for the C1 staging layer (``admin/ingest_staging.py``).

Discipline follows the task design (section 2) and the repo's e2e
conventions: every negative control passes only when the input was
actually refused, and it asserts the real filesystem state -- no new file
outside the staging root -- not just the return value. Limit boundaries
are pinned from both sides (== passes, +1 trips) with injected small
limits; the production defaults are pinned by value. Staged rows and files
on disk must stay in one-to-one correspondence across multi-round
uploads/deletes, with ``_batch.json`` re-read from disk. Memory
boundedness is shown structurally (the chunks iterator stops being
consumed at the limit break) and with a tracemalloc peak.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import tracemalloc
import uuid
import zipfile
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest

from kb_mcp_lite import docconvert
from kb_mcp_lite.admin.ingest_staging import (
    REJECTED,
    STAGED,
    Batch,
    BatchLimitError,
    BatchNotFoundError,
    ConflictingRelError,
    EmptyBatchError,
    FileRow,
    InvalidRelError,
    Limits,
    StagingStore,
    UnknownFileError,
)

#: A per-run unique stem for escape probes: a traversal rejection is also
#: proven by the absence of this exact file at the would-be escape target.
UNIQ = f"c1escape{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def make_store() -> Iterator[Callable[..., StagingStore]]:
    """Store factory whose batches never outlive the test (same cleanup
    path as the process-exit handler)."""
    made: list[StagingStore] = []

    def factory(*, limits: Limits | None = None) -> StagingStore:
        store = StagingStore(limits=limits)
        made.append(store)
        return store

    yield factory
    for store in made:
        store._cleanup_all_batches()


@pytest.fixture()
def store(make_store: Callable[..., StagingStore]) -> StagingStore:
    return make_store()


def small_limits() -> Limits:
    """Injected limits that run the production boundary math at toy sizes
    (design D-C1-3: the injection is a test seam, defaults stay pinned)."""
    return Limits(max_file_bytes=1024, max_files=3, max_total_bytes=2048)


def upload(
    store: StagingStore,
    batch: Batch,
    rel: str,
    data: bytes = b"x",
    source: str = "upload",
) -> FileRow:
    return store.add_file(batch.batch_id, rel, source, iter([data]))


def disk_files(root: Path) -> set[str]:
    """Regular files under ``<root>/files``, as posix rels -- the actual
    filesystem, not the manifest's claim about it."""
    files_root = root / "files"
    return {
        path.relative_to(files_root).as_posix()
        for path in files_root.rglob("*")
        if path.is_file()
    }


def root_entries(root: Path) -> set[str]:
    """Every path under the batch root. No ``.part`` or ``.tmp`` residue
    may ever appear here after an operation returns."""
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def read_manifest(root: Path) -> dict:
    return json.loads((root / "_batch.json").read_text("utf-8"))


def rows_from_memory(batch: Batch) -> dict[str, dict]:
    """Test-side serialization of the live manifest -- deliberately not
    ``Batch.to_dict()`` (the writer under test), so a drift between state
    and disk cannot hide behind one shared serializer."""
    return {
        rel: {
            "rel": row.rel,
            "size": row.size,
            "source": row.source,
            "state": row.state,
            "error": row.error,
        }
        for rel, row in batch.files.items()
    }


def assert_manifest_matches_memory(batch: Batch) -> None:
    manifest = read_manifest(batch.root)
    rows = manifest["files"]
    assert {row["rel"]: row for row in rows} == rows_from_memory(batch)
    assert [row["rel"] for row in rows] == list(batch.files)  # insertion order
    assert manifest["batch_id"] == batch.batch_id
    assert manifest["vault"] == batch.vault
    staged = {rel: row for rel, row in batch.files.items() if row.state == STAGED}
    assert disk_files(batch.root) == set(staged)  # staged rows <-> files, 1:1
    assert manifest["totals"] == {
        "count": len(staged),
        "bytes": sum(row.size for row in staged.values()),
    }
    assert not any(path.name.endswith(".part") for path in batch.root.rglob("*"))


def make_zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# rel path safety (design section 2.2)
# ---------------------------------------------------------------------------

INVALID_RELS: list[tuple[str, str]] = [
    ("", "empty rel"),
    ("   ", "empty rel"),
    (".", "empty rel"),
    ("a\\b.txt", "backslash in rel"),
    ("sub\\..\\x.txt", "backslash in rel"),
    ("/etc/passwd", "absolute rel"),
    ("//server/share/x.txt", "absolute rel"),
    ("C:evil.txt", "drive letter in rel"),
    ("c:/windows/x.txt", "drive letter in rel"),
    ("..", "parent traversal in rel"),
    (f"../{UNIQ}.txt", "parent traversal in rel"),
    (f"../../{UNIQ}.txt", "parent traversal in rel"),
    ("a/../b.txt", "parent traversal in rel"),
    ("a//b.txt", "empty path segment"),
    ("sub/", "empty path segment"),
]


@pytest.mark.parametrize(("rel", "reason"), INVALID_RELS)
def test_invalid_rel_is_refused_before_any_write(
    store: StagingStore, rel: str, reason: str
) -> None:
    batch = store.create_batch("vault")
    before = root_entries(batch.root)
    consumed: list[int] = []

    def chunks() -> Iterator[bytes]:
        consumed.append(1)
        yield b"should never be read"

    with pytest.raises(InvalidRelError) as excinfo:
        store.add_file(batch.batch_id, rel, "upload", chunks())

    message = str(excinfo.value)
    assert reason in message
    assert repr(rel) in message
    # The refusal happened before the body was touched at all.
    assert consumed == []
    # Actual filesystem state: nothing new anywhere under the staging root,
    # and the would-be escape target does not exist outside it either.
    assert root_entries(batch.root) == before
    assert disk_files(batch.root) == set()
    if UNIQ in rel:
        assert not (batch.root.parent / f"{UNIQ}.txt").exists()
    assert read_manifest(batch.root)["files"] == []


def test_unknown_batch_raises_by_name(store: StagingStore) -> None:
    consumed: list[int] = []

    def chunks() -> Iterator[bytes]:
        consumed.append(1)
        yield b"x"

    with pytest.raises(BatchNotFoundError) as excinfo:
        store.get_batch("nope")
    assert "nope" in str(excinfo.value)
    with pytest.raises(BatchNotFoundError) as excinfo:
        store.add_file("nope", "a.txt", "upload", chunks())
    assert "nope" in str(excinfo.value)
    with pytest.raises(BatchNotFoundError) as excinfo:
        store.remove_file("nope", "a.txt")
    assert "nope" in str(excinfo.value)
    with pytest.raises(BatchNotFoundError) as excinfo:
        store.cleanup_batch("nope")
    assert "nope" in str(excinfo.value)
    assert consumed == []


def test_remove_file_refusals_by_name(store: StagingStore) -> None:
    batch = store.create_batch("vault")
    with pytest.raises(InvalidRelError) as excinfo:
        store.remove_file(batch.batch_id, "../x.txt")
    assert "parent traversal in rel" in str(excinfo.value)
    with pytest.raises(UnknownFileError) as excinfo:
        store.remove_file(batch.batch_id, "never-uploaded.txt")
    assert "never-uploaded.txt" in str(excinfo.value)
    assert_manifest_matches_memory(batch)


# ---------------------------------------------------------------------------
# limits (design section 2.4): == passes, +1 trips
# ---------------------------------------------------------------------------


def test_file_limit_equal_passes_and_plus_one_is_rejected(
    make_store: Callable[..., StagingStore],
) -> None:
    store = make_store(limits=small_limits())
    batch = store.create_batch("vault")

    exact = upload(store, batch, "exact.bin", b"a" * 1024)
    assert exact.state == STAGED
    assert exact.size == 1024
    assert (batch.files_root / "exact.bin").stat().st_size == 1024
    assert batch.totals() == {"count": 1, "bytes": 1024}
    assert_manifest_matches_memory(batch)

    over = upload(store, batch, "over.bin", b"a" * 1025)
    assert over.state == REJECTED
    assert over.size == 1025
    assert over.error == (
        "exceeds single-file limit 1024 bytes (aborted after 1025 bytes read)"
    )
    # The batch continued; the rejected row is virtual, so totals and the
    # files on disk are untouched by it.
    assert batch.totals() == {"count": 1, "bytes": 1024}
    assert disk_files(batch.root) == {"exact.bin"}
    assert_manifest_matches_memory(batch)


def test_file_limit_stops_reading_at_the_break(
    make_store: Callable[..., StagingStore],
) -> None:
    store = make_store(limits=small_limits())
    batch = store.create_batch("vault")
    consumed: list[int] = []

    def chunks() -> Iterator[bytes]:
        for _ in range(5):
            consumed.append(1)
            yield b"b" * 800

    # 800 + 800 = 1600 crosses 1024 at the second chunk; the remaining
    # three chunks are never requested (D-C1-2: no read-to-the-end).
    row = store.add_file(batch.batch_id, "big.bin", "upload", chunks())
    assert row.state == REJECTED
    assert row.size == 1600
    assert row.error == (
        "exceeds single-file limit 1024 bytes (aborted after 1600 bytes read)"
    )
    assert len(consumed) == 2
    assert disk_files(batch.root) == set()
    assert_manifest_matches_memory(batch)


def test_batch_file_count_limit_refuses_the_next_upload(
    make_store: Callable[..., StagingStore],
) -> None:
    store = make_store(limits=small_limits())  # max_files = 3
    batch = store.create_batch("vault")
    for name in ("a.txt", "b.txt", "c.txt"):
        assert upload(store, batch, name).state == STAGED
    before = root_entries(batch.root)
    consumed: list[int] = []

    def chunks() -> Iterator[bytes]:
        consumed.append(1)
        yield b"x"

    with pytest.raises(BatchLimitError) as excinfo:
        store.add_file(batch.batch_id, "d.txt", "upload", chunks())

    err = excinfo.value
    assert err.limit == "max_files"
    assert err.limit_value == 3
    assert err.actual == 4
    # Refused before the body was read, and the refusal left no trace:
    # no manifest row, no .part, no directory churn (batch-level refusals
    # never enter the manifest -- D-C1-1).
    assert consumed == []
    assert root_entries(batch.root) == before
    assert list(batch.files) == ["a.txt", "b.txt", "c.txt"]
    assert_manifest_matches_memory(batch)


def test_batch_total_bytes_limit_refuses_mid_stream(
    make_store: Callable[..., StagingStore],
) -> None:
    store = make_store(limits=small_limits())  # max_total_bytes = 2048
    batch = store.create_batch("vault")
    assert upload(store, batch, "a.bin", b"a" * 1024).state == STAGED
    # 1024 + 1024 == the limit: equality passes.
    assert upload(store, batch, "b.bin", b"a" * 1024).state == STAGED
    assert batch.totals() == {"count": 2, "bytes": 2048}
    keep_a = (batch.files_root / "a.bin").read_bytes()

    consumed: list[int] = []

    def chunks() -> Iterator[bytes]:
        consumed.append(1)
        yield b"x"

    with pytest.raises(BatchLimitError) as excinfo:
        store.add_file(batch.batch_id, "c.txt", "upload", chunks())

    err = excinfo.value
    assert err.limit == "max_total_bytes"
    assert err.limit_value == 2048
    assert err.actual == 2049
    assert len(consumed) == 1
    # The batch and its staged files are exactly as they were (D-C1-1).
    assert (batch.files_root / "a.bin").read_bytes() == keep_a
    assert list(batch.files) == ["a.bin", "b.bin"]
    assert disk_files(batch.root) == {"a.bin", "b.bin"}
    assert not any(path.name.endswith(".part") for path in batch.root.rglob("*"))
    assert_manifest_matches_memory(batch)


def test_batch_total_limit_stops_reading_at_the_break(
    make_store: Callable[..., StagingStore],
) -> None:
    store = make_store(limits=small_limits())
    batch = store.create_batch("vault")
    assert upload(store, batch, "a1.bin", b"a" * 768).state == STAGED
    assert upload(store, batch, "a2.bin", b"a" * 768).state == STAGED  # 1536 staged
    consumed: list[int] = []

    def chunks() -> Iterator[bytes]:
        for _ in range(4):
            consumed.append(1)
            yield b"c" * 600

    with pytest.raises(BatchLimitError) as excinfo:
        store.add_file(batch.batch_id, "b.bin", "upload", chunks())

    assert excinfo.value.limit == "max_total_bytes"
    assert excinfo.value.actual == 1536 + 600
    assert len(consumed) == 1  # 2048 + 600 already over: chunk 2 never read
    assert disk_files(batch.root) == {"a1.bin", "a2.bin"}


def test_default_limits_are_pinned() -> None:
    limits = Limits()
    assert limits.max_file_bytes == 52_428_800
    assert limits.max_files == 200
    assert limits.max_total_bytes == 524_288_000
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.max_files = 1
    assert StagingStore().limits == Limits()


# ---------------------------------------------------------------------------
# rejected rows, overwrite and manifest<->disk consistency
# ---------------------------------------------------------------------------


def test_rejected_row_semantics(make_store: Callable[..., StagingStore]) -> None:
    store = make_store(limits=small_limits())
    batch = store.create_batch("vault")

    refused = upload(store, batch, "big.bin", b"a" * 2000)
    assert refused.state == REJECTED
    assert upload(store, batch, "ok.txt", b"o" * 10).state == STAGED
    assert batch.totals() == {"count": 1, "bytes": 10}  # rejected not counted

    # DELETE of a rejected (virtual) row removes just the row.
    removed = store.remove_file(batch.batch_id, "big.bin")
    assert removed.state == REJECTED
    assert "big.bin" not in batch.files

    # Re-uploading the same rel now succeeds and becomes a real file.
    again = upload(store, batch, "big.bin", b"a" * 10)
    assert again.state == STAGED
    assert disk_files(batch.root) == {"big.bin", "ok.txt"}

    # Re-uploading a staged rel with an oversized body replaces the row
    # with a rejected one -- and the old bytes leave with it, so
    # staged rows <-> files stays one-to-one.
    replaced = upload(store, batch, "big.bin", b"a" * 2000)
    assert replaced.state == REJECTED
    assert disk_files(batch.root) == {"ok.txt"}
    assert batch.totals() == {"count": 1, "bytes": 10}
    assert_manifest_matches_memory(batch)


def test_reupload_updates_row_and_bytes(store: StagingStore) -> None:
    batch = store.create_batch("vault")
    first = upload(store, batch, "sub/d/a.txt", b"x" * 5, source="upload")
    assert first.state == STAGED
    second = upload(store, batch, "sub/d/a.txt", b"y" * 9, source="folder")
    assert second.size == 9
    assert second.source == "folder"
    assert (batch.files_root / "sub/d/a.txt").read_bytes() == b"y" * 9
    assert list(batch.files) == ["sub/d/a.txt"]  # one row, position kept
    assert batch.totals() == {"count": 1, "bytes": 9}
    assert_manifest_matches_memory(batch)


def test_multiround_manifest_matches_disk(
    make_store: Callable[..., StagingStore],
) -> None:
    store = make_store(limits=small_limits())
    batch = store.create_batch("vault")
    zip_bytes = make_zip_bytes({"inner.txt": b"hello"})

    upload(store, batch, "a.txt", b"a" * 10)
    assert_manifest_matches_memory(batch)
    upload(store, batch, "sub/b.docx", b"b" * 20, source="folder")
    assert_manifest_matches_memory(batch)
    upload(store, batch, "pack.zip", zip_bytes)
    assert_manifest_matches_memory(batch)

    store.remove_file(batch.batch_id, "a.txt")
    assert_manifest_matches_memory(batch)
    upload(store, batch, "c.csv", b"c" * 5)
    assert_manifest_matches_memory(batch)
    store.remove_file(batch.batch_id, "sub/b.docx")
    assert_manifest_matches_memory(batch)

    refused = upload(store, batch, "too-big.bin", b"z" * 2000)
    assert refused.state == REJECTED
    assert_manifest_matches_memory(batch)
    store.remove_file(batch.batch_id, "too-big.bin")
    assert_manifest_matches_memory(batch)

    assert batch.totals() == {"count": 2, "bytes": len(zip_bytes) + 5}
    assert disk_files(batch.root) == {"pack.zip", "c.csv"}


def test_unicode_and_space_rels_roundtrip(store: StagingStore) -> None:
    batch = store.create_batch("vault")
    rel = "目录/中文 文件.txt"
    row = upload(store, batch, rel, b"u")
    assert row.state == STAGED
    assert disk_files(batch.root) == {rel}
    assert_manifest_matches_memory(batch)
    assert read_manifest(batch.root)["files"][0]["rel"] == rel
    removed = store.remove_file(batch.batch_id, rel)
    assert removed.rel == rel
    assert disk_files(batch.root) == set()


def test_zip_is_stored_byte_identical_and_not_unpacked(store: StagingStore) -> None:
    batch = store.create_batch("vault")
    zip_bytes = make_zip_bytes({"inner.txt": b"hello inside"})
    before = hashlib.sha256(zip_bytes).hexdigest()

    row = upload(store, batch, "pack.zip", zip_bytes)
    assert row.state == STAGED
    assert row.size == len(zip_bytes)
    staged = (batch.files_root / "pack.zip").read_bytes()
    assert hashlib.sha256(staged).hexdigest() == before
    assert staged == zip_bytes
    # Nothing was expanded: the .zip is the only file under files/.
    assert disk_files(batch.root) == {"pack.zip"}
    assert not (batch.files_root / "pack").exists()
    assert_manifest_matches_memory(batch)


# ---------------------------------------------------------------------------
# batch lifecycle and start guard (design sections 2.1 / 2.6)
# ---------------------------------------------------------------------------


def test_create_batch_layout_and_empty_manifest(store: StagingStore) -> None:
    batch = store.create_batch("my-vault")
    assert batch.root.name.startswith("kb-admin-ingest-")
    assert root_entries(batch.root) == {"files", "out", "_batch.json"}
    assert batch.files_root.is_dir()
    assert batch.out_root.is_dir()
    assert not (batch.root / "report.jsonl").exists()  # reserved for C2 only
    manifest = read_manifest(batch.root)
    assert manifest["files"] == []
    assert manifest["totals"] == {"count": 0, "bytes": 0}
    assert manifest["vault"] == "my-vault"
    assert manifest["batch_id"] == batch.batch_id
    assert datetime.fromisoformat(manifest["created_at"]).tzinfo is not None


def test_cleanup_removes_tree_and_forgets_batch(store: StagingStore) -> None:
    batch = store.create_batch("vault")
    upload(store, batch, "a.txt")
    root = batch.root
    store.cleanup_batch(batch.batch_id)
    assert not root.exists()
    with pytest.raises(BatchNotFoundError):
        store.get_batch(batch.batch_id)
    with pytest.raises(BatchNotFoundError):
        store.cleanup_batch(batch.batch_id)


def test_ensure_startable_refuses_empty_and_none_convertible(store: StagingStore) -> None:
    batch = store.create_batch("vault")
    with pytest.raises(EmptyBatchError) as excinfo:
        store.ensure_startable(batch)
    assert "nothing to convert" in str(excinfo.value)
    assert batch.batch_id in str(excinfo.value)

    upload(store, batch, "notes.rtf")
    upload(store, batch, "legacy.doc")  # the batch converter names .doc a skip
    with pytest.raises(EmptyBatchError) as excinfo:
        store.ensure_startable(batch)
    assert "nothing to convert" in str(excinfo.value)

    upload(store, batch, "doc.docx")
    store.ensure_startable(batch)  # now startable


def test_ensure_startable_rejected_rows_do_not_count(
    make_store: Callable[..., StagingStore],
) -> None:
    store = make_store(limits=small_limits())
    batch = store.create_batch("vault")
    upload(store, batch, "big.docx", b"a" * 2000)
    assert batch.files["big.docx"].state == REJECTED
    with pytest.raises(EmptyBatchError):
        store.ensure_startable(batch)


def test_ensure_startable_accepts_zip_only(store: StagingStore) -> None:
    batch = store.create_batch("vault")
    upload(store, batch, "pack.zip", b"PK\x03\x04-stub")
    store.ensure_startable(batch)  # the container suffix is convertible


# ---------------------------------------------------------------------------
# the start guard's convertible set vs the converter, sample by sample
# ---------------------------------------------------------------------------

ALIGNED_SAMPLES = [
    "a.docx", "A.DOCX", "a.Docx", "sub/dir/a.docx",
    "a.pptx", "a.PPTX",
    "a.xlsx", "a.xlsm", "A.XLS",
    "a.pdf", "a.PDF",
    "a.txt", "A.TXT",
    "a.json", "a.JSON",
    "a.md", "a.Md", "sub/a.MD",
    "a.csv", "A.CSV",
    "a.rtf", "a.doc", "A.DOC",
    "a.tar.gz", "README", "noext", ".docx",
]


def startable(store: StagingStore, batch: Batch) -> bool:
    try:
        store.ensure_startable(batch)
    except EmptyBatchError:
        return False
    return True


def docconvert_accepts(path: Path) -> bool:
    """The converter's own classification, read off its behavior: a
    refusal that is *not* the unsupported-extension one means the suffix
    was accepted (the probe environment may or may not carry the wheel,
    so a lazy-import ConversionError also means "accepted")."""
    try:
        docconvert.convert(path)
    except docconvert.ConversionError as exc:
        return "unsupported extension" not in str(exc)
    return True


@pytest.mark.parametrize("rel", ALIGNED_SAMPLES)
def test_convertible_decision_matches_docconvert(
    store: StagingStore, tmp_path: Path, rel: str
) -> None:
    batch = store.create_batch("vault")
    upload(store, batch, rel)
    probe = tmp_path / rel
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_bytes(b"x")
    got = startable(store, batch)
    expected = docconvert_accepts(probe)
    assert got == expected, f"guard={got} docconvert={expected} for {rel!r}"


@pytest.mark.parametrize("rel", ["a.zip", "A.ZIP", "pack.Zip"])
def test_zip_convertible_in_guard_refused_by_docconvert(
    store: StagingStore, tmp_path: Path, rel: str
) -> None:
    # The guard's set is SUFFIXES + {".zip"} (the batch converter expands
    # archives); docconvert itself refuses .zip by design. Both sides of
    # the deliberate divergence are pinned here.
    batch = store.create_batch("vault")
    upload(store, batch, rel)
    assert startable(store, batch) is True
    probe = tmp_path / rel
    probe.write_bytes(b"PK\x03\x04")
    assert docconvert_accepts(probe) is False
    with pytest.raises(docconvert.ConversionError) as excinfo:
        docconvert.convert(probe)
    assert "unsupported extension" in str(excinfo.value)


# ---------------------------------------------------------------------------
# memory boundedness (D-C1-5)
# ---------------------------------------------------------------------------


def test_add_file_memory_stays_bounded(store: StagingStore) -> None:
    batch = store.create_batch("vault")
    size = 8 * 1024 * 1024
    block = b"z" * (1024 * 1024)  # one reusable chunk, built before tracing

    def chunks() -> Iterator[bytes]:
        for _ in range(8):
            yield block

    tracemalloc.start()
    try:
        row = store.add_file(batch.batch_id, "big.bin", "upload", chunks())
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert row.state == STAGED
    assert row.size == size
    assert (batch.files_root / "big.bin").stat().st_size == size
    print(f"\ntracemalloc peak for {size} B upload: {peak} B")
    assert peak < size // 2  # an 8 MiB buffering implementation blows past this


# ---------------------------------------------------------------------------
# corner pins from the S2 review (design section 2.2 empirical addendum)
# ---------------------------------------------------------------------------


def test_colon_in_later_segment_refused_by_resolve_fallback(store: StagingStore) -> None:
    """``a/b:c.txt`` -- a colon in a *later* segment (Windows ADS syntax)
    slips past the named fast rules and is refused by the resolved-
    containment fallback. The message text is not pinned here: only the
    class, the basis the fallback checks, and the actual filesystem state
    inside and outside the staging root."""
    batch = store.create_batch("vault")
    before = root_entries(batch.root)
    with pytest.raises(InvalidRelError):
        store.add_file(batch.batch_id, "a/b:c.txt", "upload", iter([b"x"]))

    # The fallback's basis, recomputed: the resolved target is not inside
    # files/ (it lands on a drive-relative path -- probe-observed), so the
    # containment check, not a colon rule, is what refused it.
    target = (batch.files_root / "a/b:c.txt").resolve()
    assert not target.is_relative_to(batch.files_root.resolve())
    assert not target.exists()

    # Inside the root: nothing new, no directory churn, zero manifest rows.
    assert root_entries(batch.root) == before
    assert disk_files(batch.root) == set()
    assert not (batch.files_root / "a").exists()
    assert read_manifest(batch.root)["files"] == []


def test_windows_reserved_device_name_raises_native_oserror_without_trace(
    store: StagingStore,
) -> None:
    """``NUL`` is a Windows reserved device name -- a known corner left
    unmapped on purpose (design section 2.2). Probe-observed: the ``.part``
    open/write go to the null device, ``os.replace`` fails (WinError 183),
    and the cleanup ``unlink`` raises the native ``PermissionError`` that
    escapes -- untranslated, keeping 500 semantics. Pinned: the native
    class, zero manifest rows, no real filesystem entry under files/
    (``Path.exists`` on ``NUL`` only resolves the device; rglob sees
    nothing), and the batch still working afterwards."""
    import sys

    if sys.platform != "win32":
        pytest.skip("reserved device names are a Windows behaviour")

    batch = store.create_batch("vault")
    before = root_entries(batch.root)
    with pytest.raises(OSError) as excinfo:
        store.add_file(batch.batch_id, "NUL", "upload", iter([b"x"]))
    assert isinstance(excinfo.value, PermissionError)  # probe-pinned class

    assert root_entries(batch.root) == before
    assert disk_files(batch.root) == set()
    assert read_manifest(batch.root)["files"] == []

    # The batch is unharmed: a normal upload still works afterwards.
    assert upload(store, batch, "ok.txt").state == STAGED
    assert_manifest_matches_memory(batch)


# ---------------------------------------------------------------------------
# S5 hardening: Win32 name folding (F1 case-variant / F2 trailing space-dot)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    ["a.txt ", "dir /f.txt", "a..", "...", "x.", "a./b.txt", "a/./b"],
)
def test_trailing_space_or_dot_segment_refused(store: StagingStore, rel: str) -> None:
    """Win32 strips trailing spaces and dots per segment, so ``a.txt `` and
    ``a.txt`` (or ``a..`` and ``a``) would be one physical file -- two rows
    folded onto one file breaks the staged-row <-> disk 1:1 promise. Each
    shape is refused by name before any write; the disk state is checked
    for real, not from the error message."""
    batch = store.create_batch("vault")
    before = root_entries(batch.root)
    with pytest.raises(InvalidRelError) as excinfo:
        store.add_file(batch.batch_id, rel, "upload", iter([b"x"]))
    assert "trailing space or dot" in str(excinfo.value)
    assert repr(rel) in str(excinfo.value)

    assert root_entries(batch.root) == before
    assert disk_files(batch.root) == set()
    assert read_manifest(batch.root)["files"] == []


def test_case_variant_upload_refused_until_replace_or_delete(
    store: StagingStore,
) -> None:
    """NTFS folds ``Report.docx`` and ``report.docx`` to one file, so a
    case-variant upload would silently share the staged row's bytes. The
    refusal is by name (:class:`ConflictingRelError`); the two documented
    escape hatches still work: exact-spelling replace, and DELETE then
    upload the variant."""
    batch = store.create_batch("vault")
    row = upload(store, batch, "Report.docx", b"AAAA")
    assert row.state == STAGED

    with pytest.raises(ConflictingRelError) as excinfo:
        store.add_file(batch.batch_id, "report.docx", "upload", iter([b"zz"]))
    message = str(excinfo.value)
    assert "case-variant of staged" in message
    assert "'Report.docx'" in message
    assert repr("report.docx") in message

    # State intact: one row, one file, original bytes.
    assert list(batch.files) == ["Report.docx"]
    assert (batch.files_root / "Report.docx").read_bytes() == b"AAAA"
    assert_manifest_matches_memory(batch)

    # Exact spelling still replaces wholesale (unchanged pre-S5 semantics).
    replaced = upload(store, batch, "Report.docx", b"BBBBBB")
    assert replaced.state == STAGED and replaced.size == 6
    assert list(batch.files) == ["Report.docx"]
    assert (batch.files_root / "Report.docx").read_bytes() == b"BBBBBB"
    assert_manifest_matches_memory(batch)

    # Escape hatch: DELETE the staged row, then the variant is free.
    store.remove_file(batch.batch_id, "Report.docx")
    staged = upload(store, batch, "report.docx", b"z")
    assert staged.state == STAGED
    assert list(batch.files) == ["report.docx"]
    assert (batch.files_root / "report.docx").read_bytes() == b"z"
    assert_manifest_matches_memory(batch)


def test_case_variant_of_rejected_row_is_allowed(
    make_store: Callable[..., StagingStore],
) -> None:
    """A rejected row is virtual -- it owns no file -- so a case-variant
    upload is no physical collision and stages normally. Pins the
    staged-rows-only scope of the conflict check."""
    store = make_store(limits=Limits(max_file_bytes=8, max_files=5, max_total_bytes=100))
    batch = store.create_batch("vault")
    row = upload(store, batch, "BIG.BIN", b"0123456789")  # 10 > 8 -> rejected
    assert row.state == REJECTED

    staged = upload(store, batch, "big.bin", b"x")
    assert staged.state == STAGED
    assert [r.state for r in batch.files.values()] == [REJECTED, STAGED]
    assert_manifest_matches_memory(batch)