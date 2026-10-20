"""E2E tests for kb-mcp MCP server (Wave 2A).

Spawns ``kb serve`` as a real subprocess, speaks JSON-RPC over stdin/stdout,
and asserts tool outputs. No mocks.

MCP protocol note: tool execution errors are returned as
``{"result": {"isError": true, "content": [{"text": "..."}]}}``,
NOT as JSON-RPC ``{"error": {"code": ...}}``. The error code is embedded
in the text as ``MCP error <code>: <message>``. This matches the MCP
specification — JSON-RPC errors are for protocol-level issues, not
application-level tool failures.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from collections.abc import Iterator

import pytest

from kb_mcp_lite.md_io import parse_frontmatter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_id_counter = 0


def _next_id() -> int:
    global _id_counter
    _id_counter += 1
    return _id_counter


def _send(proc: subprocess.Popen, msg: dict[str, Any]) -> None:
    """Write a JSON-RPC message to the subprocess stdin."""
    assert proc.stdin is not None
    line = json.dumps(msg, ensure_ascii=False)
    proc.stdin.write(line + "\n")
    proc.stdin.flush()


def _recv(proc: subprocess.Popen, timeout: float = 10.0) -> dict[str, Any]:
    """Read a single JSON-RPC line from stdout."""
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        stderr = ""
        assert proc.stderr is not None
        try:
            stderr = proc.stderr.read()
        except Exception:
            pass
        raise RuntimeError(f"MCP server closed stdout (no response). stderr: {stderr[:500]}")
    return json.loads(line)


def _recv_until_id(
    proc: subprocess.Popen, expected_id: int, timeout: float = 10.0
) -> dict[str, Any]:
    """Read lines until one with the matching ``id`` arrives."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = _recv(proc, timeout=min(deadline - time.monotonic(), 5.0))
        if msg.get("id") == expected_id:
            return msg
        # Skip notifications (no id) or responses for other ids
    raise RuntimeError(f"Timeout waiting for response id={expected_id}")


def _call_tool(proc: subprocess.Popen, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Send a tools/call request and return the response."""
    rid = _next_id()
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    return _recv_until_id(proc, rid)


def _extract_result(resp: dict[str, Any]) -> Any:
    """Extract the tool result data from an MCP response.

    FastMCP wraps return values as:
        {"result": {"content": [{"type": "text", "text": <json_str>}]}}
    For list returns, FastMCP may emit one content block per item.
    For empty lists, content may be empty.
    """
    assert "result" in resp, f"expected result, got: {resp}"
    result = resp["result"]
    content = result.get("content", [])
    if not content:
        return []
    # If multiple content blocks, each is one item — collect and parse.
    if len(content) > 1:
        items = []
        for block in content:
            text = block.get("text", "")
            try:
                items.append(json.loads(text))
            except (json.JSONDecodeError, ValueError):
                items.append(text)
        return items
    # Single content block — parse as JSON (could be dict, list, or scalar).
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def _extract_error(resp: dict[str, Any]) -> tuple[int, str] | None:
    """Extract (code, message) from an MCP tool error response.

    Returns None if the response is not an error.
    The error text format is: ``Error executing tool X: MCP error <code>: <msg>``
    """
    if "result" not in resp:
        # JSON-RPC level error
        err = resp.get("error", {})
        return (err.get("code", -32603), err.get("message", ""))
    result = resp["result"]
    if not result.get("isError"):
        return None
    text = result.get("content", [{}])[0].get("text", "")
    # Parse "MCP error <code>: <msg>" from the text
    import re

    m = re.search(r"MCP error (-?\d+):\s*(.+)", text)
    if m:
        return (int(m.group(1)), m.group(2))
    return (-32603, text)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Iterator[Path]:
    """A temporary DB path isolated from the user's real KB."""
    db = tmp_path / "kb.db"
    yield db


@pytest.fixture()
def mcp_proc(tmp_db: Path) -> Iterator[subprocess.Popen]:
    """A running ``kb serve`` subprocess with a fresh temp DB."""
    global _id_counter
    _id_counter = 0  # reset per fixture

    env = {
        **os.environ,
        "KB_MCP_HOME": str(tmp_db.parent),
        "KB_MCP_LOG_LEVEL": "ERROR",
        # Isolate from the developer's embedder configuration so the
        # subprocess behaves deterministically (e.g. ``test_semantic_...``
        # expects no embedder to be configured).
        "KB_MCP_EMBEDDING_CONFIG": "",
        "KB_MCP_CONFIG": "",
        "HOME": str(tmp_db.parent),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "kb_mcp_lite.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # The server's stdio transport writes UTF-8 (measured: the Chinese
        # labels of kb_rel_spec decode as UTF-8, and cp932 fails at 0x85).
        # The locale default would break on any non-ASCII response.
        encoding="utf-8",
        env=env,
    )
    try:
        # Handshake: initialize
        rid = _next_id()
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )
        init_resp = _recv_until_id(proc, rid)
        assert "result" in init_resp, f"initialize failed: {init_resp}"

        # notifications/initialized
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        yield proc
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    """MCP server advertises the 21 kb tools."""

    def test_tools_list(self, mcp_proc: subprocess.Popen) -> None:
        """tools/list returns all registered MCP tools."""
        rid = _next_id()
        _send(mcp_proc, {"jsonrpc": "2.0", "id": rid, "method": "tools/list"})
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"tools/list failed: {resp}"
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {
            "kb_search",
            "kb_get",
            "kb_add",
            "kb_link",
            "kb_list",
            "kb_update",
            "kb_delete",
            "kb_unlink",
            "kb_history",
            "kb_restore",
            "kb_diff",
            "kb_restore_deleted",
            "kb_doctor",
            "kb_similar",
            "kb_duplicates",
            "kb_embed_status",
            "kb_embed_retry",
            "kb_diff_check",
            "kb_query_relations",
            "kb_impact",
            "kb_decision_chain",
            "kb_rel_spec",
            "kb_expand",
            "kb_schedule_list",
            "kb_schedule_status",
            "kb_schedule_run",
            "kb_schedule_history",
        }


# ---------------------------------------------------------------------------
# kb_add
# ---------------------------------------------------------------------------


class TestKbAdd:
    """kb_add creates documents and returns the generated id."""

    def test_add_happy_path(self, mcp_proc: subprocess.Popen) -> None:
        """Minimal add returns a new id."""
        resp = _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "Hello MCP"})
        data = _extract_result(resp)
        assert data["id"] == "hello-mcp"

    def test_add_with_body_and_tags(self, mcp_proc: subprocess.Popen) -> None:
        """Add with all optional fields."""
        resp = _call_tool(
            mcp_proc,
            "kb_add",
            {
                "type": "lesson",
                "title": "Body Test",
                "body": "Some content here.",
                "tags": ["test", "mcp"],
                "source": "src/x.md",
            },
        )
        data = _extract_result(resp)
        assert data["id"] == "body-test"

    def test_add_duplicate(self, mcp_proc: subprocess.Popen) -> None:
        """Adding the same (type, title) twice returns error -32005."""
        args = {"type": "project", "title": "Dup"}
        r1 = _call_tool(mcp_proc, "kb_add", args)
        assert _extract_error(r1) is None, f"first add should succeed: {r1}"
        r2 = _call_tool(mcp_proc, "kb_add", args)
        err = _extract_error(r2)
        assert err is not None, f"Expected error for duplicate, got: {r2}"
        assert err[0] == -32005, f"Expected -32005, got {err[0]}"

    def test_add_validation_error(self, mcp_proc: subprocess.Popen) -> None:
        """Empty title returns error -32602."""
        resp = _call_tool(mcp_proc, "kb_add", {"type": "project", "title": ""})
        err = _extract_error(resp)
        assert err is not None, f"Expected validation error, got: {resp}"
        assert err[0] == -32602, f"Expected -32602, got {err[0]}"


# ---------------------------------------------------------------------------
# kb_get
# ---------------------------------------------------------------------------


class TestKbGet:
    """kb_get fetches a document by id."""

    def test_get_happy_path(self, mcp_proc: subprocess.Popen) -> None:
        """Get an existing doc returns full document."""
        # First add a doc
        add_resp = _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "Get Me"})
        doc_id = _extract_result(add_resp)["id"]

        # Now get it
        resp = _call_tool(mcp_proc, "kb_get", {"id": doc_id})
        data = _extract_result(resp)
        assert data["id"] == doc_id
        assert data["title"] == "Get Me"

    def test_get_not_found(self, mcp_proc: subprocess.Popen) -> None:
        """Missing id returns error -32004."""
        resp = _call_tool(mcp_proc, "kb_get", {"id": "nonexistent-id"})
        err = _extract_error(resp)
        assert err is not None, f"Expected not_found error, got: {resp}"
        assert err[0] == -32004, f"Expected -32004, got {err[0]}"


# ---------------------------------------------------------------------------
# kb_search
# ---------------------------------------------------------------------------


class TestKbSearch:
    """kb_search performs full-text search."""

    def test_search_happy_path(self, mcp_proc: subprocess.Popen) -> None:
        """Search returns hits for matching query."""
        # Add a doc with searchable body
        _call_tool(
            mcp_proc,
            "kb_add",
            {
                "type": "project",
                "title": "Searchable",
                "body": "hello world content",
            },
        )

        resp = _call_tool(mcp_proc, "kb_search", {"query": "hello"})
        data = _extract_result(resp)
        assert isinstance(data, dict), f"Expected dict, got: {type(data)}"
        assert data["count"] >= 1, f"Expected >=1 hit, got {data['count']}"
        assert data["hits"][0]["id"] == "searchable"

    def test_search_no_results(self, mcp_proc: subprocess.Popen) -> None:
        """Search with no match returns empty hits."""
        resp = _call_tool(mcp_proc, "kb_search", {"query": "zzzzzz"})
        data = _extract_result(resp)
        assert data["count"] == 0, f"Expected 0 hits, got: {data}"
        assert data["hits"] == []

    def test_search_validation(self, mcp_proc: subprocess.Popen) -> None:
        """Empty query returns error -32602."""
        resp = _call_tool(mcp_proc, "kb_search", {"query": ""})
        err = _extract_error(resp)
        assert err is not None, f"Expected validation error, got: {resp}"
        assert err[0] == -32602, f"Expected -32602, got {err[0]}"


# ---------------------------------------------------------------------------
# kb_link
# ---------------------------------------------------------------------------


class TestKbLink:
    """kb_link creates typed edges between documents."""

    def test_link_happy_path(self, mcp_proc: subprocess.Popen) -> None:
        """Link two existing docs."""
        # Add two docs
        _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "From Doc"})
        _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "To Doc"})

        resp = _call_tool(
            mcp_proc,
            "kb_link",
            {"from_id": "from-doc", "to_id": "to-doc"},
        )
        data = _extract_result(resp)
        assert data["ok"] is True
        assert data["rel"] == "relates-to"

    def test_link_not_found(self, mcp_proc: subprocess.Popen) -> None:
        """Linking a missing doc returns error -32004."""
        resp = _call_tool(
            mcp_proc,
            "kb_link",
            {"from_id": "missing", "to_id": "also-missing"},
        )
        err = _extract_error(resp)
        assert err is not None, f"Expected not_found error, got: {resp}"
        assert err[0] == -32004, f"Expected -32004, got {err[0]}"


# ---------------------------------------------------------------------------
# Error code mapping (architecture.md § 4.4)
# ---------------------------------------------------------------------------


class TestErrorCodes:
    """Error codes match architecture.md § 4.4."""

    def test_validation_error_code(self, mcp_proc: subprocess.Popen) -> None:
        """ValidationError → -32602."""
        resp = _call_tool(mcp_proc, "kb_search", {"query": ""})
        err = _extract_error(resp)
        assert err is not None
        assert err[0] == -32602

    def test_not_found_error_code(self, mcp_proc: subprocess.Popen) -> None:
        """NotFoundError → -32004."""
        resp = _call_tool(mcp_proc, "kb_get", {"id": "missing"})
        err = _extract_error(resp)
        assert err is not None
        assert err[0] == -32004

    def test_duplicate_error_code(self, mcp_proc: subprocess.Popen) -> None:
        """DuplicateError → -32005."""
        args = {"type": "project", "title": "DupCode"}
        _call_tool(mcp_proc, "kb_add", args)
        resp = _call_tool(mcp_proc, "kb_add", args)
        err = _extract_error(resp)
        assert err is not None
        assert err[0] == -32005


# ---------------------------------------------------------------------------
# Diagnostics tools
# ---------------------------------------------------------------------------


class TestDiagnostics:
    """kb_doctor, kb_similar, and kb_duplicates work over MCP."""

    def test_semantic_mode_reaches_embedder_check(self, mcp_proc: subprocess.Popen) -> None:
        """mode='semantic' passes input validation and reports missing embedder."""
        resp = _call_tool(mcp_proc, "kb_search", {"query": "anything", "mode": "semantic"})
        err = _extract_error(resp)
        assert err is not None, f"expected error without embedder, got: {resp}"
        assert err[0] == -32602
        assert "embedder" in err[1].lower()

    def test_doctor(self, mcp_proc: subprocess.Popen) -> None:
        """kb_doctor returns a structured health report."""
        resp = _call_tool(mcp_proc, "kb_doctor", {})
        data = _extract_result(resp)
        assert isinstance(data, dict)
        assert "ok" in data
        assert isinstance(data["checks"], list)
        assert len(data["checks"]) >= 4

    def test_similar(self, mcp_proc: subprocess.Popen) -> None:
        """kb_similar must report the missing embedder, not an empty result set."""
        _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "SimilarSource"})
        resp = _call_tool(mcp_proc, "kb_similar", {"id": "similarsource"})
        err = _extract_error(resp)
        assert err is not None, f"expected error without embedder, got: {resp}"
        assert err[0] == -32602
        assert "embedder" in err[1].lower()

    def test_duplicates(self, mcp_proc: subprocess.Popen) -> None:
        """kb_duplicates must report the missing embedder, not an empty pair list."""
        _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "DupSource"})
        resp = _call_tool(mcp_proc, "kb_duplicates", {})
        err = _extract_error(resp)
        assert err is not None, f"expected error without embedder, got: {resp}"
        assert err[0] == -32602
        assert "embedder" in err[1].lower()


# ---------------------------------------------------------------------------
# Resources (v0.3)
# ---------------------------------------------------------------------------


class TestResources:
    """MCP server exposes resources via resources/list and resources/read."""

    def test_list_resources(self, mcp_proc: subprocess.Popen) -> None:
        """resources/list returns the full resource inventory."""
        rid = _next_id()
        _send(mcp_proc, {"jsonrpc": "2.0", "id": rid, "method": "resources/list"})
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"resources/list failed: {resp}"
        resources = resp["result"].get("resources", [])
        static_uris = {r["uri"] for r in resources}
        assert static_uris == {
            "kb://types",
            "kb://stats",
            "kb://list",
            "kb://changes",
        }

        rid = _next_id()
        _send(mcp_proc, {"jsonrpc": "2.0", "id": rid, "method": "resources/templates/list"})
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"resources/templates/list failed: {resp}"
        template_resources = resp["result"].get("resourceTemplates", [])
        template_uris = {r["uriTemplate"] for r in template_resources}
        assert template_uris == {
            "kb://doc/{id}",
            "kb://links/{id}",
            "kb://graph/{id}",
            "kb://graph/{id}/{depth}",
            "kb://list/{type}",
            "kb://history/{id}",
            "kb://search/{query}",
            "kb://export/{id}",
            "kb://help/{doc}",
        }
        assert len(static_uris | template_uris) == 13

    def test_read_doc_resource(self, mcp_proc: subprocess.Popen) -> None:
        """resources/read kb://doc/{id} returns the document."""
        # First add a doc
        _call_tool(
            mcp_proc, "kb_add", {"type": "project", "title": "ResourceDoc", "body": "body here"}
        )
        rid = _next_id()
        _send(
            mcp_proc,
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "resources/read",
                "params": {"uri": "kb://doc/resourcedoc"},
            },
        )
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"resources/read failed: {resp}"
        contents = resp["result"].get("contents", [])
        assert contents, f"Expected contents, got: {resp}"
        text = contents[0].get("text", "")
        doc = json.loads(text)
        assert doc.get("id") == "resourcedoc", f"Unexpected: {text[:300]}"
        assert doc.get("title") == "ResourceDoc", f"Missing title: {text[:300]}"
        assert doc.get("body") == "body here", f"Missing body: {text[:300]}"

    def test_read_doc_resource_not_found(self, mcp_proc: subprocess.Popen) -> None:
        """resources/read for a missing doc returns error JSON, not crash."""
        rid = _next_id()
        _send(
            mcp_proc,
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "resources/read",
                "params": {"uri": "kb://doc/nonexistent"},
            },
        )
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"resources/read failed: {resp}"
        contents = resp["result"].get("contents", [{}])
        text = contents[0].get("text", "")
        data = json.loads(text)
        assert data.get("error") == "not_found"

    def test_read_types_resource(self, mcp_proc: subprocess.Popen) -> None:
        """resources/read kb://types returns registered type schema."""
        rid = _next_id()
        _send(
            mcp_proc,
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "resources/read",
                "params": {"uri": "kb://types"},
            },
        )
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"resources/read failed: {resp}"
        contents = resp["result"].get("contents", [{}])
        text = contents[0].get("text", "")
        data = json.loads(text)
        assert "types" in data, f"Expected types in response, got: {text[:300]}"
        assert data["count"] >= 6, f"Expected at least 6 types, got: {data['count']}"

    def test_read_links_resource(self, mcp_proc: subprocess.Popen) -> None:
        """resources/read kb://links/{id} returns links object."""
        # Add two docs and link them
        _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "LinkA"})
        _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "LinkB"})
        _call_tool(mcp_proc, "kb_link", {"from_id": "linka", "to_id": "linkb"})

        rid = _next_id()
        _send(
            mcp_proc,
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "resources/read",
                "params": {"uri": "kb://links/linka"},
            },
        )
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"resources/read failed: {resp}"
        contents = resp["result"].get("contents", [{}])
        text = contents[0].get("text", "")
        data = json.loads(text)
        assert data.get("doc_id") == "linka", f"Unexpected: {text[:300]}"
        assert len(data.get("outlinks", [])) == 1

    def test_read_stats_resource(self, mcp_proc: subprocess.Popen) -> None:
        """resources/read kb://stats returns KB statistics."""
        rid = _next_id()
        _send(
            mcp_proc,
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "resources/read",
                "params": {"uri": "kb://stats"},
            },
        )
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"resources/read failed: {resp}"
        contents = resp["result"].get("contents", [{}])
        text = contents[0].get("text", "")
        data = json.loads(text)
        assert "total_docs" in data, f"Expected total_docs in stats, got: {text[:300]}"
        assert "docs_by_type" in data
        assert "total_links" in data

    def test_read_export_resource(self, mcp_proc: subprocess.Popen) -> None:
        """resources/read kb://export/{id} returns round-trippable Markdown."""
        _call_tool(
            mcp_proc,
            "kb_add",
            {
                "type": "project",
                "title": "ExportDoc",
                "body": "# Body",
                "tags": ["mcp"],
                "aliases": ["export-alias"],
            },
        )
        _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "ExportTarget"})
        _call_tool(
            mcp_proc,
            "kb_link",
            {"from_id": "exportdoc", "to_id": "exporttarget"},
        )

        rid = _next_id()
        _send(
            mcp_proc,
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "resources/read",
                "params": {"uri": "kb://export/exportdoc"},
            },
        )
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"resources/read failed: {resp}"
        contents = resp["result"].get("contents", [{}])
        text = contents[0].get("text", "")
        fm, body = parse_frontmatter(text)
        assert fm["title"] == "ExportDoc"
        assert fm["aliases"] == ["export-alias"]
        assert fm["links"][0]["to"] == "exporttarget"
        assert body == "# Body"

    def test_read_help_resource(self, mcp_proc: subprocess.Popen) -> None:
        """resources/read kb://help/quickstart returns packaged help."""
        rid = _next_id()
        _send(
            mcp_proc,
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "resources/read",
                "params": {"uri": "kb://help/quickstart"},
            },
        )
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"resources/read failed: {resp}"
        contents = resp["result"].get("contents", [{}])
        text = contents[0].get("text", "")
        assert text.startswith("# kb-mcp Quickstart")

    def test_read_help_resource_unknown(self, mcp_proc: subprocess.Popen) -> None:
        """resources/read for an unknown help doc returns Not Found."""
        rid = _next_id()
        _send(
            mcp_proc,
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "resources/read",
                "params": {"uri": "kb://help/does-not-exist"},
            },
        )
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"resources/read failed: {resp}"
        contents = resp["result"].get("contents", [{}])
        text = contents[0].get("text", "")
        assert "# Not Found" in text


# ---------------------------------------------------------------------------
# Prompts (v0.3)
# ---------------------------------------------------------------------------


class TestPrompts:
    """MCP server exposes prompts via prompts/list and prompts/get."""

    def test_list_prompts(self, mcp_proc: subprocess.Popen) -> None:
        """prompts/list returns the full prompt inventory."""
        rid = _next_id()
        _send(mcp_proc, {"jsonrpc": "2.0", "id": rid, "method": "prompts/list"})
        resp = _recv_until_id(mcp_proc, rid)
        assert "result" in resp, f"prompts/list failed: {resp}"
        prompts = resp["result"].get("prompts", [])
        names = {p["name"] for p in prompts}
        assert names == {
            "new-doc",
            "link-analysis",
            "search-guide",
            "import-docs",
            "doctor",
            "maintenance",
            "onboarding",
        }


# ---------------------------------------------------------------------------
# Version history tools (v0.3)
# ---------------------------------------------------------------------------


class TestKbHistory:
    """kb_history / kb_restore / kb_diff / kb_restore_deleted tools."""

    def test_history_happy_path(self, mcp_proc: subprocess.Popen) -> None:
        """kb_history returns version entries for a document."""
        _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "HistDoc"})
        _call_tool(mcp_proc, "kb_update", {"id": "histdoc", "title": "HistDoc Updated"})
        resp = _call_tool(mcp_proc, "kb_history", {"id": "histdoc"})
        data = _extract_result(resp)
        assert isinstance(data, dict), f"Expected dict, got: {type(data)}"
        assert data["count"] >= 2, f"Expected >=2 versions, got {data['count']}"

    def test_restore_happy_path(self, mcp_proc: subprocess.Popen) -> None:
        """kb_restore restores a document to a previous version."""
        _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "RestDoc", "body": "v1 body"})
        _call_tool(mcp_proc, "kb_update", {"id": "restdoc", "body": "v2 body"})
        hist_resp = _call_tool(mcp_proc, "kb_history", {"id": "restdoc"})
        hist = _extract_result(hist_resp)
        assert isinstance(hist, dict)
        versions = hist.get("history", [])
        if len(versions) >= 2:
            v1 = versions[1]["version_id"]
            resp = _call_tool(mcp_proc, "kb_restore", {"id": "restdoc", "version": v1})
            data = _extract_result(resp)
            assert isinstance(data, dict)
            assert data.get("ok") is True

    def test_diff_happy_path(self, mcp_proc: subprocess.Popen) -> None:
        """kb_diff returns field-level diff between versions."""
        _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "DiffDoc", "body": "original"})
        _call_tool(mcp_proc, "kb_update", {"id": "diffdoc", "title": "DiffDoc V2"})
        hist_resp = _call_tool(mcp_proc, "kb_history", {"id": "diffdoc"})
        hist = _extract_result(hist_resp)
        assert isinstance(hist, dict)
        versions = hist.get("history", [])
        if len(versions) >= 2:
            v1 = versions[1]["version_id"]
            v2 = versions[0]["version_id"]
            resp = _call_tool(
                mcp_proc, "kb_diff", {"id": "diffdoc", "version_a": v1, "version_b": v2}
            )
            data = _extract_result(resp)
            assert isinstance(data, dict)
            assert "changed" in data

    def test_restore_deleted_happy_path(self, mcp_proc: subprocess.Popen) -> None:
        """kb_restore_deleted restores a soft-deleted document."""
        _call_tool(mcp_proc, "kb_add", {"type": "project", "title": "DelRestore"})
        _call_tool(mcp_proc, "kb_delete", {"id": "delrestore"})
        resp = _call_tool(mcp_proc, "kb_restore_deleted", {"id": "delrestore"})
        data = _extract_result(resp)
        assert isinstance(data, dict)
        assert data.get("ok") is True
        get_resp = _call_tool(mcp_proc, "kb_get", {"id": "delrestore"})
        get_data = _extract_result(get_resp)
        assert get_data["id"] == "delrestore"


# ---------------------------------------------------------------------------
# kb_embed_status / kb_embed_retry (admin surface, embedder disabled)
# ---------------------------------------------------------------------------


class TestKbEmbedAdmin:
    def test_embed_status_reports_disabled_embedder(self, mcp_proc: subprocess.Popen) -> None:
        """kb_embed_status returns a structured report, no error."""
        resp = _call_tool(mcp_proc, "kb_embed_status", {})
        data = _extract_result(resp)
        assert isinstance(data, dict)
        assert data.get("embedder_enabled") is False
        assert data.get("queue") == {
            "pending": 0,
            "in_progress": 0,
            "done": 0,
            "failed": 0,
        }
        assert data.get("total_enqueued") == 0
        assert data.get("oldest_failed") is None

    def test_embed_retry_empty_queue(self, mcp_proc: subprocess.Popen) -> None:
        """kb_embed_retry with nothing failed is a successful no-op."""
        resp = _call_tool(mcp_proc, "kb_embed_retry", {})
        data = _extract_result(resp)
        assert data == {"ok": True, "retried": 0, "doc_id": None}

    def test_embed_retry_unknown_doc(self, mcp_proc: subprocess.Popen) -> None:
        """Retrying a doc id that was never queued moves nothing."""
        resp = _call_tool(mcp_proc, "kb_embed_retry", {"doc_id": "never/queued"})
        data = _extract_result(resp)
        assert data == {"ok": True, "retried": 0, "doc_id": "never/queued"}


# ---------------------------------------------------------------------------
# kb_rel_spec (v0.8 特性 #6)
# ---------------------------------------------------------------------------


class TestKbRelSpec:
    """The standard vocabulary through the MCP face, incl. tree ``contains``."""

    def test_contains_visible(self, mcp_proc: subprocess.Popen) -> None:
        """List mode counts 11 with contains present; show mode returns its fields."""
        resp = _call_tool(mcp_proc, "kb_rel_spec", {})
        data = _extract_result(resp)
        assert isinstance(data, dict)
        assert data["count"] == 11
        assert "contains" in {r["name"] for r in data["relations"]}

        resp = _call_tool(mcp_proc, "kb_rel_spec", {"rel_name": "contains"})
        spec = _extract_result(resp)
        assert spec["name"] == "contains"
        assert spec["forward_label"] == "包含了"
        assert spec["backward_label"] == "隶属于"
        assert spec["default_direction"] == "forward"
        assert spec["traversal_cost"] == 0.5
        assert spec["is_influence"] is False
        assert spec["is_supersession"] is False
