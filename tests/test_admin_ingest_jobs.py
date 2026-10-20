"""Unit tests for the C2 job engine (``admin/ingest_jobs.py``).

The engine runs one real ``run_ingest`` per job against a generated stub
converter (``.cmd`` + Python pair, ASCII + CRLF per repo convention;
scenario chosen by ``$KB_STUB_SCENARIO``). Because the stub writes real
report products and real Markdown trees, the import-side backfill runs the
true ``md_io`` / ``sqlite`` machinery -- the expected error strings below
are the import path's own forms (absolute ``path: message`` file errors,
file-id ``id: message`` store errors, and ``source: link a -> b (...)``
link errors), matched through the two promised head forms.

``KB_MCP_HOME`` is monkeypatched per test (the suite has no autouse
isolation), so every vault the jobs create lives under ``tmp_path``.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from kb_mcp_lite.admin.ingest_jobs import (
    CONVERTING,
    DEFAULT_TTL_SECONDS,
    DONE,
    HIDDEN_REASON,
    IMPORTING,
    LOG_TAIL_CAP,
    QUEUED,
    UNREPORTED_REASON,
    JobConflictError,
    JobNotFoundError,
    JobStore,
)
from kb_mcp_lite.admin.ingest_staging import BatchNotFoundError, StagingStore

# ---------------------------------------------------------------------------
# stub converter (.cmd + .py, generated once per test)
# ---------------------------------------------------------------------------

STUB_SOURCE = '''\
"""Job-engine stub converter; scenario chosen by $KB_STUB_SCENARIO.

Reads --out / --report from argv, plants the scenario's output tree and
report, and prints the converter's summary line.
"""
import json
import os
import sys
import time
from pathlib import Path

SCENARIO = os.environ["KB_STUB_SCENARIO"]
argv = sys.argv


def arg(name):
    return argv[argv.index(name) + 1] if name in argv else None


out = Path(arg("--out"))
out.mkdir(parents=True, exist_ok=True)
report = Path(arg("--report"))
report.parent.mkdir(parents=True, exist_ok=True)


def write_md(rel, text):
    path = out / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def record(rel, status="ok", error="", out_rel=""):
    return {
        "rel": rel,
        "kind": rel.rsplit(".", 1)[-1],
        "status": status,
        "out": out_rel,
        "id": "",
        "chars": 10 if status in ("ok", "unchanged") else 0,
        "converter": "stub",
        "lost": {},
        "warnings": [],
        "error": error,
    }


def emit(lines):
    with open(report, "a", encoding="utf-8") as sink:
        for line in lines:
            sink.write(json.dumps(line, ensure_ascii=False) + "\\n")
        sink.flush()


A_MD = "---\\nid: a\\ntype: spec\\ntitle: Doc A\\n---\\n\\nbody a\\n"
B_MD = "---\\nid: b\\ntype: spec\\ntitle: Doc B\\n---\\n\\nbody b\\n"

if SCENARIO == "ok":
    write_md("a.md", A_MD)
    write_md("b.md", B_MD)
    emit([record("a.txt", out_rel="a.md"), record("b.txt", out_rel="b.md")])
    print("2 converted, 0 failed, 0 skipped")
elif SCENARIO == "fail":
    write_md("a.md", A_MD)
    emit([record("a.txt", out_rel="a.md"),
          record("broken.docx", status="failed", error="ConversionError: boom")])
    print("[  2/2] FAIL  broken.docx -- ConversionError: boom")
    print("1 converted, 1 failed, 0 skipped")
    sys.exit(1)
elif SCENARIO == "skip":
    write_md("a.md", A_MD)
    emit([record("a.txt", out_rel="a.md"),
          record("legacy.doc", status="skipped", error="unsupported suffix: .doc")])
    print("      SKIP  legacy.doc -- unsupported suffix: .doc")
    print("1 converted, 0 failed, 1 skipped")
elif SCENARIO == "zip":
    write_md("pack.zip/sub/a.md",
             "---\\nid: pack-zip-sub-a\\ntype: spec\\ntitle: Pack A\\n---\\n\\npacked\\n")
    emit([record("pack.zip/sub/a.docx", out_rel="pack.zip/sub/a.md")])
    print("1 converted, 0 failed, 0 skipped")
elif SCENARIO == "badzip":
    emit([record("pack.zip", status="skipped", error="zip rejected: not a zip file")])
    print("      SKIP  pack.zip -- zip rejected: not a zip file")
    print("0 converted, 0 failed, 1 skipped")
elif SCENARIO == "backfill_a":
    write_md("sub/bad.md", "---\\ntitle: Broken\\n---\\n\\nbody\\n")
    emit([record("sub/bad.txt", out_rel="sub/bad.md")])
    print("1 converted, 0 failed, 0 skipped")
elif SCENARIO == "backfill_b":
    write_md("sub/a.md", "---\\ntype: spec\\ntitle: Sub A\\n---\\n\\nbody\\n")
    emit([record("sub/a.txt", out_rel="sub/a.md")])
    print("1 converted, 0 failed, 0 skipped")
elif SCENARIO == "links":
    write_md("a.md",
             "---\\nid: a\\ntype: spec\\ntitle: Doc A\\n"
             "links:\\n- to: no-such-target\\n  rel: relates-to\\n---\\n\\nbody a\\n")
    emit([record("a.txt", out_rel="a.md")])
    print("1 converted, 0 failed, 0 skipped")
elif SCENARIO == "noreport":
    write_md("a.md", A_MD)
    write_md("b.md", B_MD)
    print("2 converted, 0 failed, 0 skipped")
elif SCENARIO == "garbage_report":
    write_md("a.md", A_MD)
    write_md("b.md", B_MD)
    with open(report, "a", encoding="utf-8") as sink:
        sink.write("this is not json at all\\n")
        sink.flush()
    print("2 converted, 0 failed, 0 skipped")
elif SCENARIO == "unchanged":
    write_md("a.md", A_MD)
    emit([record("a.txt", status="unchanged", out_rel="a.md")])
    print("0 converted, 0 failed, 0 skipped, 1 unchanged")
elif SCENARIO == "flood_log":
    emit([record("many.txt", status="skipped", error="noisy")])
    for n in range(1, 601):
        print("      SKIP  f%d.xlsm -- fake skip %d" % (n, n))
    print("0 converted, 0 failed, 1 skipped")
elif SCENARIO == "rc2":
    print("stub blew up", file=sys.stderr)
    sys.exit(2)
elif SCENARIO == "progress":
    write_md("a.md", A_MD)
    write_md("b.md", B_MD)
    time.sleep(0.6)
    emit([record("a.txt", out_rel="a.md")])
    time.sleep(0.6)
    emit([record("b.txt", out_rel="b.md")])
    time.sleep(0.6)
    print("2 converted, 0 failed, 0 skipped")
else:
    raise SystemExit("unknown scenario " + SCENARIO)
'''


def make_stub(tmp_path: Path) -> Path:
    """Generate the stub converter; returns its ``.cmd`` path."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    script = stub_dir / "jobs_stub.py"
    script.write_text(STUB_SOURCE, encoding="utf-8")
    cmd = stub_dir / "jobs-stub.cmd"
    # ASCII + CRLF on purpose: the console codepage here is 932, and either
    # one alone desyncs CMD's line reader (repo convention).
    cmd.write_text(
        "@echo off\r\n"
        '"{}" "%~dp0jobs_stub.py" %*\r\n'.format(sys.executable)
        + "exit /b %ERRORLEVEL%\r\n",
        encoding="ascii",
    )
    return cmd


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_TERMINAL = (DONE, "failed")


def wait_done(jobs: JobStore, job_id: str, timeout: float = 60.0) -> dict:
    """Poll the snapshot until the job is terminal; returns it."""
    deadline = time.monotonic() + timeout
    snap = jobs.snapshot(job_id)
    while snap["state"] not in _TERMINAL and time.monotonic() < deadline:
        time.sleep(0.02)
        snap = jobs.snapshot(job_id)
    assert snap["state"] in _TERMINAL, f"job still {snap['state']!r} after {timeout}s"
    return snap


def rows_of(snap: dict) -> dict[str, dict]:
    return {row["rel"]: row for row in snap["files"]}


def rels_of(snap: dict) -> list[str]:
    return [row["rel"] for row in snap["files"]]


class Kb:
    """One isolated KB home + staging store + converter stub."""

    def __init__(self, tmp_path: Path, staging: StagingStore, cmd: Path) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "kbhome"
        self.staging = staging
        self.cmd = cmd

    def jobs(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> JobStore:
        return JobStore(self.staging, converter=str(self.cmd), ttl_seconds=ttl_seconds)

    def batch_with(self, files: dict[str, bytes], vault: str = "test-vault"):
        batch = self.staging.create_batch(vault)
        for rel, data in files.items():
            self.staging.add_file(batch.batch_id, rel, "upload", [data])
        return batch

    def scenario(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        monkeypatch.setenv("KB_STUB_SCENARIO", name)

    def run(self, monkeypatch, files, scenario, vault="test-vault", ttl_seconds=DEFAULT_TTL_SECONDS):
        """Start one job over a fresh batch and wait for its terminal state."""
        self.scenario(monkeypatch, scenario)
        batch = self.batch_with(files, vault)
        jobs = self.jobs(ttl_seconds)
        job = jobs.start(batch)
        return batch, jobs, job, wait_done(jobs, job.job_id)


@pytest.fixture()
def kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path / "kbhome"))
    cmd = make_stub(tmp_path)
    staging = StagingStore()
    yield Kb(tmp_path, staging, cmd)
    staging._cleanup_all_batches()


# ---------------------------------------------------------------------------
# the live per-file picture
# ---------------------------------------------------------------------------


def test_ok_run_full_shape(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    batch, jobs, job, snap = kb.run(monkeypatch, {"a.txt": b"a", "b.txt": b"b"}, "ok")

    assert set(snap) == {
        "job_id", "batch_id", "vault", "state", "files", "counts",
        "log_tail", "started_at", "finished_at", "error",
    }
    assert snap["job_id"] == job.job_id
    assert snap["batch_id"] == batch.batch_id
    assert snap["vault"] == "test-vault"
    assert snap["state"] == DONE
    assert snap["error"] is None
    assert snap["started_at"] is not None and snap["finished_at"] is not None
    assert snap["finished_at"] >= snap["started_at"]

    assert rels_of(snap) == ["a.txt", "b.txt"]
    for row in snap["files"]:
        assert set(row) == {"rel", "upload", "convert", "import"}
        assert row["upload"] == {"state": "done"}
        assert row["convert"] == {"state": "ok", "reason": None}
        assert row["import"] == {"state": "ok", "error": None, "posthoc": True}

    assert set(snap["counts"]) == {
        "uploaded", "converted", "failed", "skipped",
        "inserted", "updated", "import_errors",
    }
    assert snap["counts"] == {
        "uploaded": 2, "converted": 2, "failed": 0, "skipped": 0,
        "inserted": 3, "updated": 0, "import_errors": 0,
    }
    assert snap["log_tail"] == []


def test_failed_record_is_named_per_row(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    batch, jobs, job, snap = kb.run(
        monkeypatch, {"a.txt": b"a", "broken.docx": b"x"}, "fail"
    )

    # A per-file failure is a named fact; the pipeline still ran to the end.
    assert snap["state"] == DONE
    rows = rows_of(snap)
    assert rows["broken.docx"]["convert"] == {
        "state": "failed", "reason": "ConversionError: boom",
    }
    assert rows["broken.docx"]["import"] == {
        "state": "pending", "error": None, "posthoc": False,
    }
    assert rows["a.txt"]["convert"]["state"] == "ok"
    assert snap["counts"]["converted"] == 1
    assert snap["counts"]["failed"] == 1
    assert snap["log_tail"] == ["[  2/2] FAIL  broken.docx -- ConversionError: boom"]


def test_skipped_rows_and_hidden_premark(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    batch, jobs, job, snap = kb.run(
        monkeypatch,
        {"a.txt": b"a", "legacy.doc": b"d", ".hidden/h.txt": b"h"},
        "skip",
    )

    rows = rows_of(snap)
    assert rows["legacy.doc"]["convert"] == {
        "state": "skipped", "reason": "unsupported suffix: .doc",
    }
    assert rows[".hidden/h.txt"]["convert"] == {
        "state": "skipped", "reason": HIDDEN_REASON,
    }
    assert rows[".hidden/h.txt"]["import"] == {
        "state": "pending", "error": None, "posthoc": False,
    }
    assert snap["counts"]["uploaded"] == 3
    assert snap["counts"]["converted"] == 1
    assert snap["counts"]["skipped"] == 1  # converter skips only, not the hidden mark
    assert snap["counts"]["inserted"] == 2
    assert snap["log_tail"] == ["      SKIP  legacy.doc -- unsupported suffix: .doc"]


def test_zip_member_row_appended_and_pack_flipped(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    batch, jobs, job, snap = kb.run(monkeypatch, {"pack.zip": b"PK"}, "zip")

    assert rels_of(snap) == ["pack.zip", "pack.zip/sub/a.docx"]
    rows = rows_of(snap)
    assert rows["pack.zip"]["convert"] == {
        "state": "ok", "reason": "expanded: 1 members",
    }
    assert rows["pack.zip"]["import"] == {"state": "ok", "error": None, "posthoc": True}
    member = rows["pack.zip/sub/a.docx"]
    assert member["convert"] == {"state": "ok", "reason": None}
    assert member["import"] == {"state": "ok", "error": None, "posthoc": True}
    assert snap["counts"]["uploaded"] == 1
    assert snap["counts"]["converted"] == 1
    assert snap["counts"]["inserted"] == 4  # member doc + one index per directory
    assert snap["log_tail"] == []


def test_bad_zip_record_hits_the_pack_row(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    batch, jobs, job, snap = kb.run(monkeypatch, {"pack.zip": b"junk"}, "badzip")

    assert rels_of(snap) == ["pack.zip"]
    assert snap["files"][0]["convert"] == {
        "state": "skipped", "reason": "zip rejected: not a zip file",
    }
    assert snap["counts"]["skipped"] == 1
    assert snap["counts"]["inserted"] == 0
    assert snap["log_tail"] == ["      SKIP  pack.zip -- zip rejected: not a zip file"]


# ---------------------------------------------------------------------------
# import backfill
# ---------------------------------------------------------------------------


def test_backfill_file_error_hits_row_link_error_goes_to_log(
    kb: Kb, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, jobs, job, snap = kb.run(monkeypatch, {"sub/bad.txt": b"b"}, "backfill_a")

    rows = rows_of(snap)
    out_root = batch.out_root.resolve()
    # The file-level error head is the product's absolute path (md_io's form);
    # the engine resolves it against the out root and matches the reported row.
    expected = (
        f"{out_root / 'sub' / 'bad.md'}: frontmatter missing required field 'type'"
    )
    assert rows["sub/bad.txt"]["import"] == {
        "state": "failed", "error": expected, "posthoc": True,
    }
    assert snap["counts"]["import_errors"] == 2
    assert snap["counts"]["inserted"] == 2  # the two kb-index documents
    # The link error is not a product error: it is never guessed at a row.
    assert snap["log_tail"] == [
        "[import] sub\\kb-index.md: link sub/kb-index -> sub/bad (contains): "
        "document not found: 'sub/bad'"
    ]


def test_backfill_id_form_hit_with_preexisting_doc(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    from kb_mcp_lite.schema import Document
    from kb_mcp_lite.store.sqlite import SqliteStore
    from kb_mcp_lite.vault import VaultManager

    vault = "seed-vault"
    manager = VaultManager(kb.home)
    manager.create(vault)
    store = SqliteStore(manager.resolve_path(vault))
    store.add(Document(id="sub/a", type="spec", title="Sub A"))
    store.close()

    batch, jobs, job, snap = kb.run(monkeypatch, {"sub/a.txt": b"a"}, "backfill_b", vault=vault)

    rows = rows_of(snap)
    # store.import_many's error head is the file-id form (out rel minus .md).
    assert rows["sub/a.txt"]["import"] == {
        "state": "failed",
        "error": "sub/a: document already exists: 'sub/a'",
        "posthoc": True,
    }
    assert snap["counts"]["import_errors"] == 1
    assert snap["counts"]["inserted"] == 2
    assert snap["counts"]["updated"] == 0
    assert snap["log_tail"] == []


def test_link_error_miss_keeps_row_import_ok(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    batch, jobs, job, snap = kb.run(monkeypatch, {"a.txt": b"a"}, "links")

    rows = rows_of(snap)
    assert rows["a.txt"]["convert"]["state"] == "ok"
    assert rows["a.txt"]["import"] == {"state": "ok", "error": None, "posthoc": True}
    assert snap["counts"]["import_errors"] == 1
    assert snap["log_tail"] == [
        "[import] a.md: link a -> no-such-target (relates-to): "
        "document not found: 'no-such-target'"
    ]


def test_unparsable_report_rows_marked_and_counts_from_summary(
    kb: Kb, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, jobs, job, snap = kb.run(
        monkeypatch, {"a.txt": b"a", "b.txt": b"b"}, "garbage_report"
    )

    for row in snap["files"]:
        assert row["convert"] == {"state": "skipped", "reason": UNREPORTED_REASON}
        assert row["import"] == {"state": "pending", "error": None, "posthoc": False}
    assert snap["state"] == DONE
    assert snap["counts"]["converted"] == 2  # the stdout summary carries the counts
    assert snap["counts"]["skipped"] == 0
    assert snap["counts"]["failed"] == 0
    assert snap["counts"]["inserted"] == 3
    assert snap["log_tail"] == []


def test_stale_report_file_never_read(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    kb.scenario(monkeypatch, "noreport")  # writes no report at all
    batch = kb.batch_with({"a.txt": b"a", "b.txt": b"b"})
    stale = batch.root / "report.jsonl"
    stale.write_text(
        json.dumps(
            {
                "rel": "zombie.txt", "kind": "txt", "status": "ok",
                "out": "zombie.md", "id": "zombie", "chars": 1,
                "converter": "old", "lost": {}, "warnings": [], "error": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    jobs = kb.jobs()
    job = jobs.start(batch)
    snap = wait_done(jobs, job.job_id)

    assert "zombie" not in json.dumps(snap)
    assert not stale.exists()
    assert snap["counts"]["converted"] == 2
    for row in snap["files"]:
        assert row["convert"] == {"state": "skipped", "reason": UNREPORTED_REASON}


# ---------------------------------------------------------------------------
# lifecycle: states, conflict, TTL, summaries
# ---------------------------------------------------------------------------


def test_states_queued_converting_importing_done(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    kb.scenario(monkeypatch, "progress")
    batch = kb.batch_with({"a.txt": b"a", "b.txt": b"b"})
    jobs = kb.jobs()

    # Deterministic queued observation: capture the worker thread instead of
    # letting it start, so "queued" can be read before any work happens.
    captured: list = []

    class CapturedThread:
        def __init__(self, target=None, args=(), daemon=None, **kwargs):
            captured.append(self)
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self) -> None:
            pass

    real_thread = threading.Thread
    threading.Thread = CapturedThread
    try:
        job = jobs.start(batch)
        first = jobs.snapshot(job.job_id)
    finally:
        threading.Thread = real_thread

    assert first["state"] == QUEUED
    assert first["started_at"] is None
    assert first["files"][0]["convert"]["state"] == "pending"
    assert captured[0].daemon is True

    worker = real_thread(target=captured[0].target, args=captured[0].args, daemon=True)
    worker.start()
    seen: list[str] = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = jobs.snapshot(job.job_id)["state"]
        if not seen or seen[-1] != state:
            seen.append(state)
        if state in _TERMINAL:
            break
        time.sleep(0.02)

    assert seen[-1] == DONE
    assert CONVERTING in seen
    assert IMPORTING in seen
    assert set(seen) <= {QUEUED, CONVERTING, IMPORTING, DONE}
    snap = jobs.snapshot(job.job_id)
    assert [row["convert"]["state"] for row in snap["files"]] == ["ok", "ok"]


def test_conflict_names_running_job_then_rerun_allowed(
    kb: Kb, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb.scenario(monkeypatch, "progress")
    batch = kb.batch_with({"a.txt": b"a"})
    jobs = kb.jobs()

    first_job = jobs.start(batch)
    with pytest.raises(JobConflictError) as excinfo:
        jobs.start(batch)
    assert excinfo.value.job_id == first_job.job_id
    assert excinfo.value.batch_id == batch.batch_id
    assert first_job.job_id in str(excinfo.value)
    assert jobs.running_job(batch.batch_id) == first_job.job_id

    snap = wait_done(jobs, first_job.job_id)
    assert snap["state"] == DONE
    assert jobs.running_job(batch.batch_id) is None

    # A terminal batch may run again (the incremental contract's rerun).
    kb.scenario(monkeypatch, "unchanged")
    second_job = jobs.start(batch)
    snap2 = wait_done(jobs, second_job.job_id)
    assert snap2["state"] == DONE
    assert snap2["files"][0]["convert"]["state"] == "unchanged"
    assert jobs.running_job(batch.batch_id) is None


def test_ttl_keeps_running_batch_then_reaps_it(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    kb.scenario(monkeypatch, "progress")
    batch = kb.batch_with({"a.txt": b"a", "b.txt": b"b"})
    jobs = kb.jobs(ttl_seconds=0.05)

    job = jobs.start(batch)
    time.sleep(0.4)
    summaries = jobs.summaries()  # mid-run: a non-terminal job keeps it alive
    assert [s["job_id"] for s in summaries] == [job.job_id]
    assert summaries[0]["state"] == CONVERTING
    assert batch.root.exists()

    deadline = time.monotonic() + 60
    while batch.root.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
        jobs.summaries()  # the sweep is lazy: reads drive it
    assert not batch.root.exists()
    assert jobs.summaries() == []
    with pytest.raises(JobNotFoundError):
        jobs.snapshot(job.job_id)
    with pytest.raises(BatchNotFoundError):
        kb.staging.get_batch(batch.batch_id)


def test_start_after_ttl_keeps_the_batch_being_started(
    kb: Kb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rerun is not abandonment: the sweep at ``start`` must not reap the
    very batch being started (the TTL clock restarts with the new job)."""
    kb.scenario(monkeypatch, "ok")
    batch = kb.batch_with({"a.txt": b"a"})
    jobs = kb.jobs(ttl_seconds=0.2)
    first = jobs.start(batch)
    wait_done(jobs, first.job_id)
    time.sleep(0.3)  # past the TTL: the next call would sweep this batch

    kb.scenario(monkeypatch, "unchanged")
    second = jobs.start(batch)  # starting it must not sweep it first
    snap = wait_done(jobs, second.job_id)

    assert snap["state"] == DONE
    assert rows_of(snap)["a.txt"]["convert"]["state"] == "unchanged"
    kb.staging.get_batch(batch.batch_id)  # still registered (no BatchNotFound)
    assert (batch.files_root / "a.txt").is_file()  # and its tree survived


def test_summaries_newest_first_and_light(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    kb.scenario(monkeypatch, "ok")
    b1 = kb.batch_with({"a.txt": b"a"}, vault="vault-a")
    b2 = kb.batch_with({"b.txt": b"b"}, vault="vault-b")
    jobs = kb.jobs()

    j1 = jobs.start(b1)
    wait_done(jobs, j1.job_id)
    j2 = jobs.start(b2)
    wait_done(jobs, j2.job_id)

    summaries = jobs.summaries()
    assert [s["job_id"] for s in summaries] == [j2.job_id, j1.job_id]
    assert [s["state"] for s in summaries] == [DONE, DONE]
    assert summaries[0]["vault"] == "vault-b"
    for s in summaries:
        assert set(s) == {
            "job_id", "batch_id", "vault", "state", "counts",
            "started_at", "finished_at", "error",
        }


# ---------------------------------------------------------------------------
# incremental-status counts and the log-tail cap
# ---------------------------------------------------------------------------


def test_unchanged_record_counts_and_product(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    batch, jobs, job, snap = kb.run(monkeypatch, {"a.txt": b"a"}, "unchanged")

    row = snap["files"][0]
    assert row["convert"] == {"state": "unchanged", "reason": None}
    assert row["import"] == {"state": "ok", "error": None, "posthoc": True}
    assert snap["counts"]["converted"] == 0
    assert snap["counts"]["skipped"] == 0
    assert snap["counts"]["inserted"] == 2
    assert snap["log_tail"] == []


def test_log_tail_cap_keeps_the_tail(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    batch, jobs, job, snap = kb.run(monkeypatch, {"many.txt": b"m"}, "flood_log")

    assert snap["counts"]["skipped"] == 1
    assert len(snap["log_tail"]) == LOG_TAIL_CAP
    assert snap["log_tail"][0] == "      SKIP  f101.xlsm -- fake skip 101"
    assert snap["log_tail"][-1] == "      SKIP  f600.xlsm -- fake skip 600"


# ---------------------------------------------------------------------------
# pipeline failure
# ---------------------------------------------------------------------------


def test_converter_crash_fails_the_job(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    kb.scenario(monkeypatch, "rc2")
    batch = kb.batch_with({"a.txt": b"a"})
    jobs = kb.jobs()
    job = jobs.start(batch)
    snap = wait_done(jobs, job.job_id)

    assert snap["state"] == "failed"
    assert snap["error"].startswith("ConverterRunError:")
    assert "exit code 2" in snap["error"]
    assert "stub blew up" in snap["error"]
    assert snap["finished_at"] is not None
    row = snap["files"][0]
    assert row["convert"] == {"state": "pending", "reason": None}
    assert snap["counts"]["uploaded"] == 1
    assert snap["counts"]["converted"] == 0