"""Root-path and breadcrumb-parity tests for the Jinja admin console home.

Born with task 10-16-kb-admin-app-bridge (2026-10-16): ``GET /`` must serve
the Jinja overview directly -- the historical 302 -> ``/app`` redirect is
gone -- and ``spa_router.js`` must carry the same Chinese breadcrumb map as
``base.html`` (the router re-sets the breadcrumb on every client-side
navigation). t2 pins the deliberate boundary that ``/app`` stays reachable
(frozen SPA, not retired). Every assertion is context-anchored -- a header,
an opening tag, a ``const pathMap = {...};`` block -- so a check can only
fail on the artifact it is meant to examine.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kb_mcp_lite.admin import STATIC_DIR, TEMPLATES_DIR, create_app
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore


def make_store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "kb.db")
    store.add(Document(id="proj/sample", type="project", title="Sample Project"))
    return store


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """The real app over a throwaway store -- never the real KB home."""
    return TestClient(create_app(store=make_store(tmp_path)))


def parse_pathmap(text: str) -> dict[str, str]:
    """The single ``const pathMap = {...};`` block of a page source."""
    match = re.search(r"const pathMap = \{(.*?)\};", text, re.S)
    assert match is not None, "no `const pathMap = {...};` block in the text"
    return dict(re.findall(r"'([^']+)':\s*'([^']+)'", match.group(1)))


# ---------------------------------------------------------------------------
# R1 -- the root path serves the Jinja overview
# ---------------------------------------------------------------------------


def test_root_serves_jinja_overview(client: TestClient) -> None:
    # follow_redirects=False is load-bearing: if the 302 -> /app ever comes
    # back, a following client would land on the SPA's 200 and fake a pass.
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 200
    assert "location" not in res.headers
    assert 'href="/ingest"' in res.text  # C3 nav registration on the page
    assert "有效文档" in res.text  # overview stat label


# ---------------------------------------------------------------------------
# R4 -- /app stays reachable (frozen, not retired)
# ---------------------------------------------------------------------------


def test_app_console_still_reachable(client: TestClient) -> None:
    res = client.get("/app")
    assert res.status_code == 200
    assert "kb-mcp Console" in res.text


# ---------------------------------------------------------------------------
# R2 -- breadcrumb map parity between base.html and spa_router.js
# ---------------------------------------------------------------------------


def test_breadcrumb_pathmap_parity() -> None:
    base_text = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    router_text = (STATIC_DIR / "spa_router.js").read_text(encoding="utf-8")

    base_map = parse_pathmap(base_text)
    router_map = parse_pathmap(router_text)

    assert router_map == base_map, "spa_router.js pathMap drifted from base.html"
    assert len(router_map) == 11
    assert router_map["/ingest"] == "转换"

    # Fallback labels, same words as base.html; the negative asserts pin the
    # old English labels out (they only ever appeared as these literals).
    assert "'新建文档'" in router_text
    assert "'编辑文档'" in router_text
    assert "pageName = '管理';" in router_text
    assert "'Admin'" not in router_text
    assert "'Overview'" not in router_text
    assert "'New Document'" not in router_text