import pytest
from pathlib import Path
from kb_mcp_lite.schema import Document
from kb_mcp_lite.vault import VaultManager
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.mcp_server import _make_server


def test_multi_vault_mcp_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path))
    vm = VaultManager(tmp_path)
    vm.create("work")
    vm.create("personal")

    # Add doc to work vault
    store_work = SqliteStore(vm.resolve_path("work"))
    store_work.add(
        Document(
            id="proj/corp-api",
            type="project",
            title="Corp API",
            body="Company internal microservice.",
        )
    )
    store_work.close()

    # Add doc to personal vault
    store_personal = SqliteStore(vm.resolve_path("personal"))
    store_personal.add(
        Document(id="proj/blog", type="project", title="My Blog", body="Personal static site.")
    )
    store_personal.close()

    # Create server
    mcp = _make_server()

    # Test kb_get by explicit vault
    doc_work = mcp._tool_manager._tools["kb_get"].fn(id="proj/corp-api", vault="work")
    assert doc_work["id"] == "proj/corp-api"
    assert doc_work["title"] == "Corp API"

    doc_personal = mcp._tool_manager._tools["kb_get"].fn(id="proj/blog", vault="personal")
    assert doc_personal["id"] == "proj/blog"
    assert doc_personal["title"] == "My Blog"

    # Test kb_search across all vaults via '*'
    all_search = mcp._tool_manager._tools["kb_search"].fn(query="API", vault="*")
    assert all_search["count"] >= 1
    assert any(h["id"] == "proj/corp-api" and h["vault"] == "work" for h in all_search["hits"])

    # Test kb_list by vault
    list_work = mcp._tool_manager._tools["kb_list"].fn(vault="work")
    assert any(d["id"] == "proj/corp-api" for d in list_work["documents"])
    assert not any(d["id"] == "proj/blog" for d in list_work["documents"])
