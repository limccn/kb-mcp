"""API tests for the C1 ingest staging routes (``admin/routes_ingest.py``).

Same discipline as the domain suite (``test_admin_ingest_staging.py``):
every negative control passes only when the input was actually refused --
status, ``ok is False``, and the real filesystem state, read from the
batch root behind ``app.state.staging``. Limits are injected small
(design D-C1-3) so the batch-level 409s run the full boundary math
cheaply; the production defaults are pinned by value through
``GET /api/ingest/limits``. ``httpx`` collapses literal ``..`` path
segments before sending, so a DELETE that must carry a traversal keeps it
percent-encoded (``%2e%2e``); the multipart ``rel`` form field reaches
the domain verbatim and needs no such care.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb_mcp_lite.admin import create_app
from kb_mcp_lite.admin.ingest_staging import Limits
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore

#: A per-run unique stem: a refused traversal is also proven by the
#: absence of this exact file at the would-be escape target.
UNIQ = f"c1route{uuid.uuid4().hex[:10]}"


def small_limits() -> Limits:
    """Toy caps that run the production boundary math at small sizes."""
    return Limits(max_file_bytes=1024, max_files=3, max_total_bytes=2048)


def make_store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "kb.db")
    store.add(Document(id="proj/sample", type="project", title="Sample Project"))
    return store


@pytest.fixture()
def app_client(tmp_path: Path) -> Iterator[tuple[FastAPI, TestClient]]:
    """The real app on a tmp store with toy limits; every batch the test
    created is removed at teardown (same path as process exit)."""
    app = create_app(store=make_store(tmp_path), staging_limits=small_limits())
    client = TestClient(app)
    yield app, client
    app.state.staging._cleanup_all_batches()


def disk_files(root: Path) -> set[str]:
    """Regular files under ``<batch root>/files`` -- the actual filesystem,
    not the manifest's claim about it."""
    files_root = root / "files"
    return {
        path.relative_to(files_root).as_posix() for path in files_root.rglob("*") if path.is_file()
    }


def root_entries(root: Path) -> set[str]:
    """Every path under the batch root: no ``.part``/``.tmp`` residue may
    appear after an operation returns."""
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def new_batch(client: TestClient) -> str:
    res = client.post("/api/ingest/batches", json={"vault": "vault"})
    assert res.status_code == 201
    return res.json()["batch_id"]


def upload(
    client: TestClient,
    batch_id: str,
    rel: str,
    data: bytes = b"x",
    source: str | None = None,
    filename: str | None = None,
):
    form = {"rel": rel}
    if source is not None:
        form["source"] = source
    return client.post(
        f"/api/ingest/batches/{batch_id}/files",
        files={"file": (filename or "upload.bin", data, "application/octet-stream")},
        data=form,
    )


# ---------------------------------------------------------------------------
# POST /api/ingest/batches
# ---------------------------------------------------------------------------


def test_create_batch_shape_and_explicit_vault(app_client) -> None:
    app, client = app_client
    res = client.post("/api/ingest/batches", json={"vault": "my-vault"})
    assert res.status_code == 201
    payload = res.json()
    assert payload["ok"] is True
    assert payload["vault"] == "my-vault"
    assert payload["limits"] == {
        "max_file_bytes": 1024,
        "max_files": 3,
        "max_total_bytes": 2048,
    }
    assert payload["totals"] == {"count": 0, "bytes": 0}
    assert isinstance(payload["batch_id"], str) and payload["batch_id"]

    batch = app.state.staging.get_batch(payload["batch_id"])
    assert batch.vault == "my-vault"
    assert batch.root.name.startswith("kb-admin-ingest-")
    assert (batch.root / "_batch.json").is_file()


def test_create_batch_resolves_default_vault_to_current(
    app_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app, client = app_client
    # Isolate vault resolution from the machine's real KB home/registry:
    # a fresh KB_MCP_HOME names its only vault "default", and clearing
    # KB_MCP_VAULT keeps the registry (not an env leak) in charge.
    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path / "kbhome"))
    monkeypatch.delenv("KB_MCP_VAULT", raising=False)

    # A missing body, an empty JSON body and an empty vault string all name
    # the same vault: the process's current one.
    for body in (None, {}, {"vault": ""}):
        res = (
            client.post("/api/ingest/batches")
            if body is None
            else client.post("/api/ingest/batches", json=body)
        )
        assert res.status_code == 201
        assert res.json()["vault"] == "default"


# ---------------------------------------------------------------------------
# POST /api/ingest/batches/{id}/files
# ---------------------------------------------------------------------------


def test_upload_staged_shape_disk_and_defaults(app_client) -> None:
    app, client = app_client
    batch_id = new_batch(client)

    res = upload(
        client,
        batch_id,
        "sub/dir/a.txt",
        b"hello",
        source="folder",
        filename="client-name.txt",
    )
    assert res.status_code == 201
    payload = res.json()
    assert payload["ok"] is True
    assert payload["file"] == {
        "rel": "sub/dir/a.txt",
        "size": 5,
        "source": "folder",
        "state": "staged",
        "error": None,
    }
    assert payload["totals"] == {"count": 1, "bytes": 5}
    batch = app.state.staging.get_batch(batch_id)
    # The rel form field is authoritative: the client filename is ignored.
    assert (batch.files_root / "sub/dir/a.txt").read_bytes() == b"hello"
    assert disk_files(batch.root) == {"sub/dir/a.txt"}

    # source defaults to "upload" and the row shape stays exactly five keys.
    res = upload(client, batch_id, "b.txt", b"bee")
    assert set(res.json()["file"]) == {"rel", "size", "source", "state", "error"}
    assert res.json()["file"]["source"] == "upload"
    assert res.json()["totals"] == {"count": 2, "bytes": 8}


def test_upload_missing_file_is_422_and_empty_rel_is_named_400(app_client) -> None:
    app, client = app_client
    batch_id = new_batch(client)

    # `file` is required by the multipart contract: FastAPI's own 422.
    res = client.post(f"/api/ingest/batches/{batch_id}/files", data={"rel": "a.txt"})
    assert res.status_code == 422

    # A missing or blank rel carries no file name -- refused by name (400).
    for form in ({}, {"rel": "   "}):
        res = client.post(
            f"/api/ingest/batches/{batch_id}/files",
            files={"file": ("a.txt", b"x", "text/plain")},
            data=form,
        )
        assert res.status_code == 400
        body = res.json()
        assert body["ok"] is False
        assert "empty rel" in body["error"]

    batch = app.state.staging.get_batch(batch_id)
    assert batch.files == {}
    assert disk_files(batch.root) == set()


def test_upload_rejects_bad_source_by_name(app_client) -> None:
    app, client = app_client
    batch_id = new_batch(client)

    res = upload(client, batch_id, "a.txt", b"x", source="scp")
    assert res.status_code == 400
    body = res.json()
    assert body["ok"] is False
    assert "invalid source" in body["error"] and "'scp'" in body["error"]
    batch = app.state.staging.get_batch(batch_id)
    assert batch.files == {}
    assert disk_files(batch.root) == set()

    # Both accepted values go through.
    for source in ("upload", "folder"):
        assert upload(client, batch_id, f"{source}.txt", b"x", source=source).status_code == 201


TRAVERSAL_RELS = [
    (f"../{UNIQ}.txt", "parent traversal in rel"),
    (f"../../{UNIQ}.txt", "parent traversal in rel"),
    (f"..\\{UNIQ}.txt", "backslash in rel"),
    (f"/{UNIQ}.txt", "absolute rel"),
    (f"C:{UNIQ}.txt", "drive letter in rel"),
    (f"sub/../../{UNIQ}.txt", "parent traversal in rel"),
    ("a//b.txt", "empty path segment"),
]


def test_upload_traversal_rels_refused_staging_root_untouched(app_client) -> None:
    app, client = app_client
    batch_id = new_batch(client)
    batch = app.state.staging.get_batch(batch_id)
    before = root_entries(batch.root)

    for rel, reason in TRAVERSAL_RELS:
        res = upload(client, batch_id, rel, b"should never land")
        assert res.status_code == 400
        body = res.json()
        assert body["ok"] is False
        assert reason in body["error"]
        assert repr(rel) in body["error"]

    # Actual filesystem state: nothing new anywhere under the batch root,
    # and the would-be escape targets do not exist outside it either.
    assert root_entries(batch.root) == before
    assert disk_files(batch.root) == set()
    assert batch.files == {}
    assert not (batch.root / f"{UNIQ}.txt").exists()
    assert not (batch.root.parent / f"{UNIQ}.txt").exists()


def test_upload_over_file_limit_is_200_rejected_and_batch_continues(app_client) -> None:
    app, client = app_client
    batch_id = new_batch(client)

    res = upload(client, batch_id, "big.bin", b"a" * 1025)
    assert res.status_code == 200  # not an exception -- a rejected row
    body = res.json()
    assert body["ok"] is False
    assert body["file"] == {
        "rel": "big.bin",
        "size": 1025,
        "source": "upload",
        "state": "rejected",
        "error": "exceeds single-file limit 1024 bytes (aborted after 1025 bytes read)",
    }
    assert body["totals"] == {"count": 0, "bytes": 0}  # rejected is not counted

    # The batch continued; the rejected row is virtual: DELETE drops the row.
    assert upload(client, batch_id, "ok.txt", b"o").status_code == 201
    deleted = client.delete(f"/api/ingest/batches/{batch_id}/files/big.bin")
    assert deleted.status_code == 200
    assert deleted.json() == {
        "ok": True,
        "removed": True,
        "totals": {"count": 1, "bytes": 1},
    }
    batch = app.state.staging.get_batch(batch_id)
    assert disk_files(batch.root) == {"ok.txt"}
    assert list(batch.files) == ["ok.txt"]


def test_batch_file_count_limit_409_names_the_limit(app_client) -> None:
    app, client = app_client  # max_files = 3
    batch_id = new_batch(client)
    for name in ("a.txt", "b.txt", "c.txt"):
        assert upload(client, batch_id, name, b"x").status_code == 201

    res = upload(client, batch_id, "d.txt", b"x")
    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False
    assert body["limit"] == "max_files"
    assert body["limit_value"] == 3
    assert body["actual"] == 4
    assert "max_files" in body["error"]
    # Refused before entering the manifest: no row, no filesystem trace.
    batch = app.state.staging.get_batch(batch_id)
    assert list(batch.files) == ["a.txt", "b.txt", "c.txt"]
    assert disk_files(batch.root) == {"a.txt", "b.txt", "c.txt"}
    assert batch.totals() == {"count": 3, "bytes": 3}


def test_batch_total_bytes_limit_409_names_the_limit(app_client) -> None:
    app, client = app_client  # max_total_bytes = 2048, max_file_bytes = 1024
    batch_id = new_batch(client)
    assert upload(client, batch_id, "a.bin", b"a" * 1024).status_code == 201
    # 1024 + 1024 == the limit: equality passes.
    assert upload(client, batch_id, "b.bin", b"a" * 1024).status_code == 201

    res = upload(client, batch_id, "c.txt", b"c")
    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False
    assert body["limit"] == "max_total_bytes"
    assert body["limit_value"] == 2048
    assert body["actual"] == 2049  # the break point, not the true body size
    batch = app.state.staging.get_batch(batch_id)
    assert list(batch.files) == ["a.bin", "b.bin"]
    assert disk_files(batch.root) == {"a.bin", "b.bin"}
    assert not any(path.name.endswith(".part") for path in batch.root.rglob("*"))


def test_unknown_batch_404_on_all_batch_scoped_routes(app_client) -> None:
    _app, client = app_client
    ghost = "no-such-batch"

    res = upload(client, ghost, "a.txt", b"x")
    assert res.status_code == 404
    assert res.json()["ok"] is False and ghost in res.json()["error"]

    res = client.delete(f"/api/ingest/batches/{ghost}/files/a.txt")
    assert res.status_code == 404
    assert res.json()["ok"] is False and ghost in res.json()["error"]


# ---------------------------------------------------------------------------
# DELETE /api/ingest/batches/{id}/files/{rel}
# ---------------------------------------------------------------------------


def test_delete_multilevel_cjk_and_reuse(app_client) -> None:
    app, client = app_client
    batch_id = new_batch(client)
    rel = "目录/中文 文件.txt"

    assert upload(client, batch_id, rel, b"u", source="folder").status_code == 201
    batch = app.state.staging.get_batch(batch_id)

    res = client.delete(f"/api/ingest/batches/{batch_id}/files/{rel}")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "removed": True, "totals": {"count": 0, "bytes": 0}}
    assert disk_files(batch.root) == set()

    # Deleting it again: the batch exists, the row does not -> named 404.
    again = client.delete(f"/api/ingest/batches/{batch_id}/files/{rel}")
    assert again.status_code == 404
    assert again.json()["ok"] is False
    assert repr(rel) in again.json()["error"]

    # The rel is free again: re-uploading it after the delete works.
    assert upload(client, batch_id, rel, b"v").status_code == 201
    assert disk_files(batch.root) == {rel}


def test_delete_traversal_refused_via_encoded_path(app_client) -> None:
    app, client = app_client
    batch_id = new_batch(client)
    batch = app.state.staging.get_batch(batch_id)
    before = root_entries(batch.root)

    # httpx collapses a literal ".." before sending, so the traversal
    # travels percent-encoded and starlette decodes it for the route.
    res = client.delete(f"/api/ingest/batches/{batch_id}/files/%2e%2e/{UNIQ}.txt")
    assert res.status_code == 400
    assert res.json()["ok"] is False
    assert "parent traversal in rel" in res.json()["error"]
    assert root_entries(batch.root) == before
    assert not (batch.root / f"{UNIQ}.txt").exists()
    assert not (batch.root.parent / f"{UNIQ}.txt").exists()


# ---------------------------------------------------------------------------
# GET /api/ingest/limits
# ---------------------------------------------------------------------------


def test_limits_endpoint_defaults_pinned(tmp_path: Path) -> None:
    client = TestClient(create_app(store=make_store(tmp_path)))
    res = client.get("/api/ingest/limits")
    assert res.status_code == 200
    assert res.json() == {
        "limits": {
            "max_file_bytes": 52_428_800,  # 50 MiB
            "max_files": 200,
            "max_total_bytes": 524_288_000,  # 500 MiB
        }
    }


def test_limits_endpoint_reflects_injected_limits(app_client) -> None:
    _app, client = app_client
    res = client.get("/api/ingest/limits")
    assert res.status_code == 200
    assert res.json() == {
        "limits": {"max_file_bytes": 1024, "max_files": 3, "max_total_bytes": 2048}
    }


# ---------------------------------------------------------------------------
# manifest <-> disk across multiple rounds through the API
# ---------------------------------------------------------------------------


def test_multiround_manifest_matches_disk(app_client) -> None:
    app, client = app_client
    batch_id = new_batch(client)
    zip_bytes = b"PK\x03\x04stub-archive-bytes"

    assert upload(client, batch_id, "a.txt", b"a" * 10).status_code == 201
    assert upload(client, batch_id, "sub/b.docx", b"b" * 20, source="folder").status_code == 201
    assert upload(client, batch_id, "pack.zip", zip_bytes).status_code == 201

    assert client.delete(f"/api/ingest/batches/{batch_id}/files/a.txt").status_code == 200
    assert upload(client, batch_id, "c.csv", b"c" * 5).status_code == 201
    assert client.delete(f"/api/ingest/batches/{batch_id}/files/sub/b.docx").status_code == 200
    rejected = upload(client, batch_id, "too-big.bin", b"z" * 2000)
    assert rejected.status_code == 200
    assert rejected.json()["file"]["state"] == "rejected"
    assert rejected.json()["file"]["size"] == 2000  # the break count
    assert client.delete(f"/api/ingest/batches/{batch_id}/files/too-big.bin").status_code == 200

    batch = app.state.staging.get_batch(batch_id)
    manifest = json.loads((batch.root / "_batch.json").read_text("utf-8"))
    rows = {row["rel"]: row for row in manifest["files"]}
    assert set(rows) == {"pack.zip", "c.csv"}
    assert all(row["state"] == "staged" for row in rows.values())
    assert manifest["totals"] == {"count": 2, "bytes": len(zip_bytes) + 5}
    assert batch.totals() == manifest["totals"]
    # Staged rows <-> files on disk, one-to-one; the .zip stayed whole.
    assert disk_files(batch.root) == {"pack.zip", "c.csv"}
    assert (batch.files_root / "pack.zip").read_bytes() == zip_bytes
    assert not any(path.name.endswith(".part") for path in batch.root.rglob("*"))


# ---------------------------------------------------------------------------
# S5 hardening over HTTP: Win32 name folding (F1 case-variant / F2 space-dot)
# ---------------------------------------------------------------------------


def read_manifest_rows(batch) -> list[str]:
    manifest = json.loads((batch.root / "_batch.json").read_text("utf-8"))
    return [row["rel"] for row in manifest["files"]]


def test_case_variant_and_trailing_space_refused_via_api(app_client) -> None:
    """Both S5 refusals travel the domain's InvalidRelError -> 400 mapping
    (ConflictingRelError is a subclass): the wire says ``ok: false`` and
    names the reason, and the staging tree is untouched for real."""
    app, client = app_client
    batch_id = new_batch(client)
    batch = app.state.staging.get_batch(batch_id)
    assert upload(client, batch_id, "Report.docx", b"AAAA").status_code == 201

    res = upload(client, batch_id, "report.docx", b"zz")
    assert res.status_code == 400
    payload = res.json()
    assert payload["ok"] is False
    assert "case-variant of staged 'Report.docx'" in payload["error"]
    assert list(batch.files) == ["Report.docx"]
    assert disk_files(batch.root) == {"Report.docx"}
    assert (batch.files_root / "Report.docx").read_bytes() == b"AAAA"

    res = upload(client, batch_id, "a.txt ", b"x")
    assert res.status_code == 400
    assert res.json()["ok"] is False
    assert "trailing space or dot" in res.json()["error"]
    assert disk_files(batch.root) == {"Report.docx"}
    assert read_manifest_rows(batch) == ["Report.docx"]
