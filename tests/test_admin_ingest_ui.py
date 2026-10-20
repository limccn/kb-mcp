"""SSR smoke tests for the C3 ingest page (``GET /ingest``).

The page's JavaScript is deliberately not exercised here (design D-C3-4
draws the line: browser behavior is the human-acceptance surface, and C4's
e2e is HTTP-level too). These tests pin what the server renders: the shell,
the controls the JS hooks onto, the seven stage columns, and the three
navigation registrations in ``base.html``. Every assertion is
context-anchored -- an opening tag, a pathMap pair, a palette entry -- so a
check can only fail on the artifact it is meant to examine.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kb_mcp_lite.admin import create_app
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore


def make_store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "kb.db")
    store.add(Document(id="proj/sample", type="project", title="Sample Project"))
    return store


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """The real app over a throwaway store. The page under test is SSR-only
    (no batch is created), so there is no staging tree to clean up."""
    return TestClient(create_app(store=make_store(tmp_path)))


def get_ingest_html(client: TestClient) -> str:
    res = client.get("/ingest")
    assert res.status_code == 200
    return res.text


def opening_tag(text: str, element_id: str) -> str:
    """The opening tag carrying ``id="<element_id>"`` -- attributes are
    asserted on that exact element, never on the page at large."""
    match = re.search(rf'<[a-z]+[^>]*\bid="{re.escape(element_id)}"[^>]*>', text)
    assert match is not None, f"no element with id={element_id!r} in the page"
    return match.group(0)


def root_html(client: TestClient) -> str:
    """The SSR root render.

    ``GET /`` serves the Jinja overview directly (no SPA redirect since
    2026-10-16, task 10-16-kb-admin-app-bridge) -- same ``base.html``, so the
    navigation registrations are carried by this response.
    """
    res = client.get("/")
    assert res.status_code == 200
    return res.text


# ---------------------------------------------------------------------------
# GET /ingest -- the page shell
# ---------------------------------------------------------------------------


def test_ingest_page_shell_and_controls(client: TestClient) -> None:
    text = get_ingest_html(client)
    assert "<title>转换 · kb-mcp admin</title>" in text
    assert "<h1>转换 / Ingest</h1>" in text

    # Both entry points (prd Req 2): multi-select files, and the directory
    # input whose `webkitdirectory` attribute the JS reads
    # (`webkitRelativePath`) to keep the selected structure as `rel`.
    file_input = opening_tag(text, "file-input")
    assert "multiple" in file_input
    dir_input = opening_tag(text, "dir-input")
    assert "webkitdirectory" in dir_input and "multiple" in dir_input

    # The JS anchors: the start button (disabled until there are rows, which
    # is also the double-click guard), the list container and the log tail.
    assert "disabled" in opening_tag(text, "start-btn")
    assert 'id="rows"' in text
    assert 'id="log-tail"' in text


def test_limits_api_still_served(client: TestClient) -> None:
    """Registering the page route must not disturb the C1/C2 API surface."""
    res = client.get("/api/ingest/limits")
    assert res.status_code == 200
    assert set(res.json()["limits"]) == {"max_file_bytes", "max_files", "max_total_bytes"}


def test_file_table_has_seven_stage_headers(client: TestClient) -> None:
    """The list is a seven-column grid (path/size/upload/convert/import/
    reason/actions); the header labels are the page's contract with the JS
    row renderer. ``bar-track`` is the reused progress-bar hook the upload
    column draws per file."""
    text = get_ingest_html(client)
    labels = re.findall(r'<div class="ingest-col">([^<]+)</div>', text)
    assert labels == ["路径", "大小", "上传", "转换", "入库", "原因", "操作"]
    assert "bar-track" in text


# ---------------------------------------------------------------------------
# base.html navigation -- three registrations, three taps, three tests
# (one poison per test keeps each red set exactly one test wide)
# ---------------------------------------------------------------------------


def test_nav_sidebar_registers_ingest_link(client: TestClient) -> None:
    html = root_html(client)
    match = re.search(r'<a href="/ingest" class="nav-link"[^>]*>(.*?)</a>', html, re.S)
    assert match is not None, "sidebar nav lacks an /ingest link"
    assert "转换" in match.group(1)


def test_pathmap_has_ingest_breadcrumb(client: TestClient) -> None:
    html = root_html(client)
    assert "'/ingest': '转换'" in html, "pathMap lacks the /ingest breadcrumb entry"


def test_command_palette_has_ingest_page(client: TestClient) -> None:
    html = root_html(client)
    match = re.search(r"\{ name: '[^']*', url: '/ingest', icon: 'refresh-cw' \}", html)
    assert match is not None, "command palette pages[] lacks an /ingest entry"
    assert "'refresh-cw':" in html, "palette icon map lacks the refresh-cw icon"