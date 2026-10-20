from __future__ import annotations

import io
import zipfile
from pathlib import Path
import pytest

from fastapi.testclient import TestClient

from kb_mcp_lite.admin import create_app
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore


def make_store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "kb.db")
    store.add(
        Document(
            id="proj/sample",
            type="project",
            title="Sample Project",
            body="SQLite FTS and MCP integration",
            tags=["sqlite", "mcp"],
        )
    )
    store.add(
        Document(
            id="lesson/failure-mode",
            type="lesson",
            title="Failure mode",
            body="Agents should consult the knowledge base before editing.",
            tags=["agents"],
        )
    )
    store.link("lesson/failure-mode", "proj/sample", rel="relates-to")
    return store


def test_overview_renders(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    # Root serves the Jinja overview directly; no 302 -> /app
    # (2026-10-16, task 10-16-kb-admin-app-bridge).
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "有效文档" in response.text
    assert "Sample Project" in response.text


def test_document_create_and_update(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    create_response = client.post(
        "/documents",
        data={
            "id": "",
            "type": "faq",
            "title": "How do we ship?",
            "tags": "release, process",
            "source": "",
            "body": "Run tests first.",
        },
        follow_redirects=False,
    )

    assert create_response.status_code == 303
    created_id = "how-do-we-ship"
    assert store.get(created_id).title == "How do we ship?"

    update_response = client.post(
        f"/documents/{created_id}",
        data={
            "title": "How do we ship safely?",
            "tags": "release,process",
            "source": "docs/release.md",
            "body": "Run tests and verify migrations.",
            "action": "save",
        },
        follow_redirects=False,
    )

    assert update_response.status_code == 303
    updated_doc = store.get(created_id)
    assert updated_doc.title == "How do we ship safely?"
    assert updated_doc.source == "docs/release.md"


def test_search_lab_and_import_export(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    search_response = client.get("/search", params={"query": "SQLite", "mode": "hybrid"})
    assert search_response.status_code == 200
    assert "Sample Project" in search_response.text

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as zf:
        zf.writestr(
            "entry.md",
            "---\n"
            "type: project\n"
            "title: Imported Project\n"
            "tags: [imported]\n"
            "---\n\n"
            "Imported body.\n",
        )
    archive_buffer.seek(0)

    import_response = client.post(
        "/imports",
        files={"archive": ("vault.zip", archive_buffer.getvalue(), "application/zip")},
        data={},
    )
    assert import_response.status_code == 200
    assert "Imported Project" not in import_response.text
    # Now filename = id: the zip contains "entry.md" → id = "entry"
    assert store.get("entry").title == "Imported Project"

    export_response = client.post("/exports")
    assert export_response.status_code == 200
    assert "已写入" in export_response.text


def test_admin_json_api(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    stats_response = client.get("/api/stats")
    assert stats_response.status_code == 200
    stats_payload = stats_response.json()
    assert stats_payload["stats"]["documents"] == 2

    docs_response = client.get("/api/docs", params={"q": "SQLite"})
    assert docs_response.status_code == 200
    docs_payload = docs_response.json()
    assert docs_payload["count"] >= 1
    assert any(item["id"] == "proj/sample" for item in docs_payload["items"])

    health_response = client.get("/api/health")
    assert health_response.status_code == 200
    health_payload = health_response.json()
    assert "checks" in health_payload
    assert "audit_log" in health_payload

    links_response = client.get("/api/links")
    assert links_response.status_code == 200
    links_payload = links_response.json()
    assert links_payload["count"] == 1
    assert links_payload["items"][0]["to_id"] == "proj/sample"


def test_spa_router_waits_for_external_scripts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    response = client.get("/static/spa_router.js")

    assert response.status_code == 200
    assert "async executeScripts(scripts)" in response.text
    assert "newScript.async = false" in response.text


def test_links_page_and_link_mutation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    page_response = client.get("/links")
    assert page_response.status_code == 200
    assert "关系列表" in page_response.text

    create_response = client.post(
        "/links",
        data={
            "from_id": "proj/sample",
            "to_id": "lesson/failure-mode",
            "rel": "references",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    assert any(link.rel == "references" for link in store.outlinks("proj/sample"))

    delete_response = client.post(
        "/links/delete",
        data={
            "from_id": "proj/sample",
            "to_id": "lesson/failure-mode",
            "rel": "references",
        },
        follow_redirects=False,
    )
    assert delete_response.status_code == 303
    assert not any(link.rel == "references" for link in store.outlinks("proj/sample"))


def test_admin_json_write_api(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    create_response = client.post(
        "/api/docs",
        json={
            "type": "faq",
            "title": "How do we release?",
            "tags": ["release", "ops"],
            "source": "docs/release.md",
            "body": "Run tests and verify the changelog.",
        },
    )
    assert create_response.status_code == 201
    created_payload = create_response.json()
    created_id = created_payload["doc"]["id"]
    assert created_payload["ok"] is True
    assert store.get(created_id).title == "How do we release?"

    patch_response = client.patch(
        f"/api/docs/{created_id}",
        json={
            "title": "How do we release safely?",
            "tags": ["release", "ops", "checklist"],
        },
    )
    assert patch_response.status_code == 200
    patched_payload = patch_response.json()
    assert patched_payload["doc"]["title"] == "How do we release safely?"
    assert "checklist" in patched_payload["doc"]["tags"]

    delete_response = client.request("DELETE", f"/api/docs/{created_id}")
    assert delete_response.status_code == 200
    deleted_payload = delete_response.json()
    assert deleted_payload["doc"]["deleted_at"] is not None

    detail_response = client.get(f"/api/docs/{created_id}")
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert len(detail_payload["history"]) >= 3

    link_response = client.post(
        "/api/links",
        json={
            "from_id": "proj/sample",
            "to_id": "lesson/failure-mode",
            "rel": "references",
        },
    )
    assert link_response.status_code == 201
    assert any(link.rel == "references" for link in store.outlinks("proj/sample"))

    unlink_response = client.request(
        "DELETE",
        "/api/links",
        json={
            "from_id": "proj/sample",
            "to_id": "lesson/failure-mode",
            "rel": "references",
        },
    )
    assert unlink_response.status_code == 200
    assert unlink_response.json()["removed"] == 1


def test_flash_feedback_and_history_render(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    create_response = client.post(
        "/documents",
        data={
            "id": "",
            "type": "faq",
            "title": "Flash doc",
            "tags": "ui",
            "source": "",
            "body": "hello",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert "Document created" in create_response.text
    assert "版本历史" in create_response.text


def test_scheduler_page_and_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    # Page render
    res = client.get("/scheduler")
    assert res.status_code == 200
    assert "调度管理" in res.text

    # Status API
    status_res = client.get("/api/scheduler/status")
    assert status_res.status_code == 200
    assert "running" in status_res.json()

    # Tasks API
    tasks_res = client.get("/api/scheduler/tasks")
    assert tasks_res.status_code == 200
    assert tasks_res.json()["ok"] is True
    assert len(tasks_res.json()["tasks"]) >= 1

    # History API
    hist_res = client.get("/api/scheduler/history")
    assert hist_res.status_code == 200
    assert hist_res.json()["ok"] is True

    # Run task API
    run_res = client.post("/api/scheduler/run", json={"task_name": "auto-reindex"})
    assert run_res.status_code == 200
    assert run_res.json()["ok"] is True
    assert run_res.json()["task_name"] == "auto-reindex"


def test_settings_page_and_embed_api(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    # Page render
    res = client.get("/settings")
    assert res.status_code == 200
    assert "设置与健康" in res.text
    assert "嵌入队列" in res.text

    # Embed status API
    status_res = client.get("/api/embed/status")
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert status_data["ok"] is True
    assert "pending" in status_data
    assert "in_progress" in status_data
    assert "done" in status_data
    assert "failed" in status_data

    # Embed retry API
    retry_res = client.post("/api/embed/retry", json={})
    assert retry_res.status_code == 200
    assert retry_res.json()["ok"] is True
    assert "retried" in retry_res.json()


def test_types_page_and_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Use isolated config path in tmp_path
    monkeypatch.setenv("KB_MCP_CONFIG", str(tmp_path / "config.yaml"))

    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    # 1. Page render
    res = client.get("/types")
    assert res.status_code == 200
    assert "文档类型管理" in res.text
    assert "项目/规划" in res.text

    # 2. List API
    list_res = client.get("/api/types")
    assert list_res.status_code == 200
    list_data = list_res.json()
    assert list_data["ok"] is True
    assert list_data["stats"]["builtin"] >= 9
    assert any(t["name"] == "project" for t in list_data["types"])

    # 3. Create custom type
    create_res = client.post(
        "/api/types",
        json={
            "name": "requirement",
            "label": "需求文档",
            "description": "产品功能规格与变更记录",
            "color": "#0284c7",
        },
    )
    assert create_res.status_code == 201
    assert create_res.json()["ok"] is True
    assert create_res.json()["type"]["name"] == "requirement"

    # Cannot recreate built-in or duplicate type
    dup_res = client.post(
        "/api/types",
        json={"name": "requirement", "label": "重复需求"},
    )
    assert dup_res.status_code == 409

    builtin_dup = client.post(
        "/api/types",
        json={"name": "project", "label": "项目"},
    )
    assert builtin_dup.status_code == 409

    # 4. Update custom type
    update_res = client.put(
        "/api/types/requirement",
        json={
            "label": "产品需求规格",
            "description": "需求规格说明书",
            "color": "#0369a1",
        },
    )
    assert update_res.status_code == 200
    assert update_res.json()["ok"] is True
    assert update_res.json()["type"]["label"] == "产品需求规格"

    # 5. Delete built-in type forbidden
    del_builtin_res = client.delete("/api/types/project")
    assert del_builtin_res.status_code == 400

    # 5.1 Delete custom type with active documents forbidden
    store.add(Document(id="req-1", type="requirement", title="Auth Requirement"))
    del_in_use_res = client.delete("/api/types/requirement")
    assert del_in_use_res.status_code == 400
    assert "正在使用" in del_in_use_res.json()["error"]

    # 6. Delete custom type succeeds once documents are removed
    store.delete("req-1")
    del_custom_res = client.delete("/api/types/requirement")
    assert del_custom_res.status_code == 200
    assert del_custom_res.json()["ok"] is True
    assert del_custom_res.json()["deleted"] == "requirement"


def test_type_display_labels_in_ui(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    # Check documents list table renders type display name
    docs_res = client.get("/documents")
    assert docs_res.status_code == 200
    assert "项目/规划" in docs_res.text
    assert "经验复盘" in docs_res.text

    # Check search page dropdown options
    search_res = client.get("/search")
    assert search_res.status_code == 200
    assert "项目/规划 (project)" in search_res.text
    assert "经验复盘 (lesson)" in search_res.text

    # Check document detail page
    detail_res = client.get("/documents/proj/sample")
    assert detail_res.status_code == 200
    assert "项目/规划 (project)" in detail_res.text


def test_spa_routes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    # Check SPA index route
    app_res = client.get("/app")
    assert app_res.status_code == 200
    assert "kb-mcp Console" in app_res.text
    assert "root" in app_res.text

    # Check SPA sub-route
    sub_res = client.get("/app/docs")
    assert sub_res.status_code == 200
    assert "kb-mcp Console" in sub_res.text


def test_git_diff_api(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    diff_res = client.get("/api/git/diff")
    assert diff_res.status_code == 200
    payload = diff_res.json()
    assert payload["ok"] is True
    assert "diff" in payload
    assert "pending_diffs" in payload
