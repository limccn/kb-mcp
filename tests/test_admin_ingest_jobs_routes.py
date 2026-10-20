"""API tests for the C2 job routes (``routes_ingest.py`` + app wiring).

Same discipline as the C1 route suite: every negative control passes only
when the input was actually refused -- status, ``ok is False``, and the
real filesystem / manifest state behind ``app.state.staging``. Jobs run a
generated stub converter (``.cmd`` + Python pair, ASCII + CRLF; scenario
chosen by ``$KB_STUB_SCENARIO``), so the engine really converts, imports
into a throwaway vault (``KB_MCP_HOME`` under ``tmp_path``), and finishes
on its own -- the guard tests read that live non-terminal window.

``ingest_converter`` / ``ingest_ttl_seconds`` are the app's C2 test
seams; everything else goes through the real ``create_app``.
"""

from __future__ import annotations

import re
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb_mcp_lite.admin import create_app
from kb_mcp_lite.admin.ingest_jobs import DEFAULT_TTL_SECONDS
from kb_mcp_lite.admin.ingest_staging import BatchNotFoundError, Limits
from kb_mcp_lite.store.sqlite import SqliteStore

# ---------------------------------------------------------------------------
# stub converter (.cmd + .py, generated once per test)
# ---------------------------------------------------------------------------

STUB_SOURCE = '''\
"""Route-suite stub converter; scenario chosen by $KB_STUB_SCENARIO."""
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

A_MD = "---\\nid: a\\ntype: spec\\ntitle: Doc A\\n---\\n\\nbody a\\n"
B_MD = "---\\nid: b\\ntype: spec\\ntitle: Doc B\\n---\\n\\nbody b\\n"


def write_md(rel, text):
    path = out / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def record(rel, out_rel):
    return {
        "rel": rel, "kind": "txt", "status": "ok", "out": out_rel, "id": "",
        "chars": 10, "converter": "stub", "lost": {}, "warnings": [], "error": "",
    }


def emit(lines):
    with open(report, "a", encoding="utf-8") as sink:
        for line in lines:
            sink.write(json.dumps(line, ensure_ascii=False) + "\\n")
        sink.flush()


if SCENARIO == "ok":
    write_md("a.md", A_MD)
    write_md("b.md", B_MD)
    emit([record("a.txt", "a.md"), record("b.txt", "b.md")])
    print("2 converted, 0 failed, 0 skipped")
elif SCENARIO == "progress":
    write_md("a.md", A_MD)
    write_md("b.md", B_MD)
    time.sleep(0.6)
    emit([record("a.txt", "a.md")])
    time.sleep(0.6)
    emit([record("b.txt", "b.md")])
    time.sleep(0.6)
    print("2 converted, 0 failed, 0 skipped")
else:
    raise SystemExit("unknown scenario " + SCENARIO)
'''


def make_stub(tmp_path: Path) -> Path:
    """Generate the stub converter; returns its ``.cmd`` path."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    script = stub_dir / "routes_stub.py"
    script.write_text(STUB_SOURCE, encoding="utf-8")
    cmd = stub_dir / "routes-stub.cmd"
    # ASCII + CRLF on purpose: the console codepage here is 932, and either
    # one alone desyncs CMD's line reader (repo convention).
    cmd.write_text(
        "@echo off\r\n"
        '"{}" "%~dp0routes_stub.py" %*\r\n'.format(sys.executable)
        + "exit /b %ERRORLEVEL%\r\n",
        encoding="ascii",
    )
    return cmd


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def small_limits() -> Limits:
    """Toy caps that run the production boundary math at small sizes."""
    return Limits(max_file_bytes=1024, max_files=3, max_total_bytes=2048)


@pytest.fixture()
def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Factory: a real app on a tmp store + stub converter per call."""
    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path / "kbhome"))
    cmd = make_stub(tmp_path)
    created: list[FastAPI] = []

    def build(ttl_seconds: float = DEFAULT_TTL_SECONDS) -> tuple[FastAPI, TestClient]:
        app = create_app(
            store=SqliteStore(tmp_path / "kb.db"),
            staging_limits=small_limits(),
            ingest_converter=str(cmd),
            ingest_ttl_seconds=ttl_seconds,
        )
        created.append(app)
        return app, TestClient(app)

    yield build
    for app in created:
        app.state.staging._cleanup_all_batches()


def disk_files(root: Path) -> set[str]:
    """Regular files under ``<batch root>/files`` -- the actual filesystem."""
    files_root = root / "files"
    return {
        path.relative_to(files_root).as_posix()
        for path in files_root.rglob("*")
        if path.is_file()
    }


def root_entries(root: Path) -> set[str]:
    """Every path under the batch root: no ``.part``/``.tmp`` residue."""
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def new_batch(client: TestClient) -> str:
    res = client.post("/api/ingest/batches", json={"vault": "vault"})
    assert res.status_code == 201
    return res.json()["batch_id"]


def upload(client: TestClient, batch_id: str, rel: str, data: bytes = b"x"):
    return client.post(
        f"/api/ingest/batches/{batch_id}/files",
        files={"file": (rel.rsplit("/", 1)[-1], data, "application/octet-stream")},
        data={"rel": rel},
    )


def start(client: TestClient, batch_id: str):
    return client.post(f"/api/ingest/batches/{batch_id}/start")


def wait_job_done(client: TestClient, job_id: str, timeout: float = 60.0) -> dict:
    """Poll the detail route until terminal; returns the full job body."""
    deadline = time.monotonic() + timeout
    body = client.get(f"/api/ingest/jobs/{job_id}").json()
    while body.get("state") not in ("done", "failed") and time.monotonic() < deadline:
        time.sleep(0.02)
        body = client.get(f"/api/ingest/jobs/{job_id}").json()
    assert body.get("state") in ("done", "failed"), f"job still {body.get('state')!r}"
    return body


def run_ok_job(client: TestClient, scenario_env: pytest.MonkeyPatch) -> dict:
    """One finished ok-scenario job over a fresh two-file batch."""
    scenario_env.setenv("KB_STUB_SCENARIO", "ok")
    batch_id = new_batch(client)
    assert upload(client, batch_id, "a.txt", b"a").status_code == 201
    assert upload(client, batch_id, "b.txt", b"b").status_code == 201
    res = start(client, batch_id)
    assert res.status_code == 202
    return wait_job_done(client, res.json()["job_id"])


# ---------------------------------------------------------------------------
# POST /api/ingest/batches/{id}/start
# ---------------------------------------------------------------------------


def test_start_unknown_batch_is_404(make_app) -> None:
    _app, client = make_app()
    res = start(client, uuid.uuid4().hex)
    assert res.status_code == 404
    body = res.json()
    assert body["ok"] is False
    assert "unknown batch" in body["error"]


def test_start_empty_and_unconvertible_batch_is_409(
    make_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, client = make_app()
    monkeypatch.setenv("KB_STUB_SCENARIO", "ok")

    batch_id = new_batch(client)
    res = start(client, batch_id)
    assert res.status_code == 409
    assert res.json()["ok"] is False
    assert "nothing to convert" in res.json()["error"]

    # Staged but nothing the converter takes: .doc is a named skip upstream,
    # so the batch must be refused here rather than exit-2 in the worker.
    assert upload(client, batch_id, "legacy.doc", b"d").status_code == 201
    res = start(client, batch_id)
    assert res.status_code == 409
    assert "convertible suffix" in res.json()["error"]


def test_start_returns_202_job_shape(make_app, monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client = make_app()
    monkeypatch.setenv("KB_STUB_SCENARIO", "ok")
    batch_id = new_batch(client)
    assert upload(client, batch_id, "a.txt", b"a").status_code == 201
    assert upload(client, batch_id, "b.txt", b"b").status_code == 201

    res = start(client, batch_id)
    assert res.status_code == 202
    payload = res.json()
    assert set(payload) == {"ok", "job_id", "batch_id", "state"}
    assert payload["ok"] is True
    assert payload["batch_id"] == batch_id
    assert payload["state"] in ("queued", "converting")
    assert re.fullmatch(r"[0-9a-f]{32}", payload["job_id"])

    body = wait_job_done(client, payload["job_id"])
    assert body["state"] == "done"
    assert body["batch_id"] == batch_id
    assert body["vault"] == "vault"


def test_start_conflict_names_running_job(make_app, monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client = make_app()
    monkeypatch.setenv("KB_STUB_SCENARIO", "progress")
    batch_id = new_batch(client)
    assert upload(client, batch_id, "a.txt", b"a").status_code == 201
    assert upload(client, batch_id, "b.txt", b"b").status_code == 201

    first = start(client, batch_id)
    assert first.status_code == 202
    job_id = first.json()["job_id"]

    res = start(client, batch_id)
    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False
    assert body["job_id"] == job_id
    assert job_id in body["error"]

    assert wait_job_done(client, job_id)["state"] == "done"

    # A terminal batch may start again (the incremental rerun contract).
    second = start(client, batch_id)
    assert second.status_code == 202
    assert wait_job_done(client, second.json()["job_id"])["state"] == "done"


# ---------------------------------------------------------------------------
# GET /api/ingest/jobs and /api/ingest/jobs/{id}
# ---------------------------------------------------------------------------


def test_jobs_list_newest_first_and_light(make_app, monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client = make_app()
    first = run_ok_job(client, monkeypatch)
    second = run_ok_job(client, monkeypatch)

    res = client.get("/api/ingest/jobs")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    jobs = body["jobs"]
    assert [j["job_id"] for j in jobs] == [second["job_id"], first["job_id"]]
    for job in jobs:
        assert "files" not in job and "log_tail" not in job
        assert set(job) == {
            "job_id", "batch_id", "vault", "state", "counts",
            "started_at", "finished_at", "error",
        }


def test_job_detail_is_full_shape_and_unknown_is_404(
    make_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, client = make_app()
    done = run_ok_job(client, monkeypatch)

    res = client.get(f"/api/ingest/jobs/{done['job_id']}")
    assert res.status_code == 200
    body = res.json()
    # The full form is the job JSON verbatim (parent design section 3).
    assert set(body) == {
        "job_id", "batch_id", "vault", "state", "files", "counts",
        "log_tail", "started_at", "finished_at", "error",
    }
    assert [row["rel"] for row in body["files"]] == ["a.txt", "b.txt"]
    assert body["counts"]["inserted"] == 3
    assert body["state"] == "done"

    res = client.get(f"/api/ingest/jobs/{uuid.uuid4().hex}")
    assert res.status_code == 404
    assert res.json()["ok"] is False
    assert "unknown job" in res.json()["error"]


# ---------------------------------------------------------------------------
# the file guards
# ---------------------------------------------------------------------------


def test_upload_refused_while_job_running(make_app, monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app()
    monkeypatch.setenv("KB_STUB_SCENARIO", "progress")
    batch_id = new_batch(client)
    assert upload(client, batch_id, "a.txt", b"a").status_code == 201
    assert upload(client, batch_id, "b.txt", b"b").status_code == 201
    res = start(client, batch_id)
    assert res.status_code == 202
    job_id = res.json()["job_id"]

    batch = app.state.staging.get_batch(batch_id)

    # Let the job's own first artifact land (the stub mkdirs out/) before the
    # before-snapshot: its next write is the first report emit 0.6s later,
    # far outside the refusal's window -- without the sync the snapshot pair
    # races the child's startup and would flake.
    deadline = time.monotonic() + 30
    while not (batch.root / "out").exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    before_manifest = sorted(batch.files)
    before_disk = disk_files(batch.root)
    before_entries = root_entries(batch.root)

    res = upload(client, batch_id, "extra.txt", b"y")
    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False
    assert body["job_id"] == job_id
    assert job_id in body["error"]
    # Negative control: refused means nothing changed -- manifest, bytes, or
    # residue anywhere under the batch root.
    assert sorted(batch.files) == before_manifest
    assert disk_files(batch.root) == before_disk
    assert root_entries(batch.root) == before_entries

    assert wait_job_done(client, job_id)["state"] == "done"
    # The guard lifts with the job: the same upload now lands normally.
    res = upload(client, batch_id, "extra.txt", b"y")
    assert res.status_code == 201
    assert res.json()["ok"] is True


def test_delete_refused_while_job_running(make_app, monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app()
    monkeypatch.setenv("KB_STUB_SCENARIO", "progress")
    batch_id = new_batch(client)
    assert upload(client, batch_id, "a.txt", b"a").status_code == 201
    assert upload(client, batch_id, "b.txt", b"b").status_code == 201
    res = start(client, batch_id)
    assert res.status_code == 202
    job_id = res.json()["job_id"]

    batch = app.state.staging.get_batch(batch_id)
    res = client.delete(f"/api/ingest/batches/{batch_id}/files/a.txt")
    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False
    assert body["job_id"] == job_id
    assert job_id in body["error"]
    # Negative control: the row and its bytes are still exactly in place.
    assert "a.txt" in batch.files
    assert (batch.files_root / "a.txt").is_file()

    assert wait_job_done(client, job_id)["state"] == "done"
    res = client.delete(f"/api/ingest/batches/{batch_id}/files/a.txt")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert "a.txt" not in batch.files


def test_unknown_batch_still_404_before_the_guard(make_app, monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client = make_app()
    monkeypatch.setenv("KB_STUB_SCENARIO", "ok")
    ghost = uuid.uuid4().hex
    res = upload(client, ghost, "a.txt", b"a")
    assert res.status_code == 404
    assert "unknown batch" in res.json()["error"]
    res = client.delete(f"/api/ingest/batches/{ghost}/files/a.txt")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# TTL sweeping through the routes
# ---------------------------------------------------------------------------


def test_ttl_get_jobs_sweeps_and_batch_dir_gone(
    make_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = make_app(ttl_seconds=0.05)
    monkeypatch.setenv("KB_STUB_SCENARIO", "progress")
    batch_id = new_batch(client)
    assert upload(client, batch_id, "a.txt", b"a").status_code == 201
    assert upload(client, batch_id, "b.txt", b"b").status_code == 201
    res = start(client, batch_id)
    assert res.status_code == 202
    job_id = res.json()["job_id"]
    batch = app.state.staging.get_batch(batch_id)

    time.sleep(0.4)
    res = client.get("/api/ingest/jobs")  # mid-run: the job keeps it alive
    jobs = res.json()["jobs"]
    assert [j["job_id"] for j in jobs] == [job_id]
    assert jobs[0]["state"] == "converting"
    assert batch.root.exists()

    deadline = time.monotonic() + 60
    while batch.root.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
        client.get("/api/ingest/jobs")  # the sweep is lazy: reads drive it
    assert not batch.root.exists()
    assert client.get("/api/ingest/jobs").json()["jobs"] == []
    assert client.get(f"/api/ingest/jobs/{job_id}").status_code == 404
    with pytest.raises(BatchNotFoundError):
        app.state.staging.get_batch(batch_id)