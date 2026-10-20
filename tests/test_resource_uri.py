"""Resource-layer URI handling: hierarchical ids, percent-encoding, graph arity.

Every case drives a real ``_make_server()`` instance **in process** and awaits
``read_resource`` directly — the same call the low-level server makes for
``resources/read``. Keeping it off the wire avoids the cp932 subprocess decode
fails that already sit in ``tests/test_mcp_e2e.py``; the wire-level coverage
stays in ``kbmcp/e2e/run.py``.

The defect this pins down: ``ResourceTemplate.matches`` (mcp 1.x) turns every
``{x}`` into ``[^/]+``, so a template parameter cannot span a slash and is never
percent-decoded — while kb ids are ``type/slug`` (the shape ``kb_add`` generates
by default).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kb_mcp_lite.mcp_server import _make_server, _normalize_resource_uri
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.vault import VaultManager, get_current_vault_name

DEC = "dec/e2e-harness"  # hierarchical id: the shape kb_add generates by default
FLAT = "e2e-flat"  # no slash — the control that always worked
JP_DOC = "dec/jp"
JP_QUERY = "督促状"
JP_QUERY_PCT = "%E7%9D%A3%E4%BF%83%E7%8A%B6"


def read(mcp: object, uri: str) -> str:
    """Read one resource in-process; return the text of its single content."""
    contents = asyncio.run(mcp.read_resource(uri))  # type: ignore[attr-defined]
    return contents[0].content


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path))
    monkeypatch.delenv("KB_MCP_VAULT", raising=False)
    vm = VaultManager(tmp_path)
    store = SqliteStore(vm.resolve_path("default"))
    try:
        store.add(Document(id=DEC, type="decision", title="e2e 决策", body="层级 id 正文。"))
        store.add(Document(id=FLAT, type="project", title="扁平对照", body="无斜杠正文。"))
        store.add(Document(id="dec/jp", type="decision", title="督促状一括引抜",
                           body="督促状は受注確定時に行う。"))
    finally:
        store.close()
    return _make_server()


def test_hierarchical_id_matches_doc_and_links_templates(server) -> None:
    doc = json.loads(read(server, f"kb://doc/{DEC}"))
    assert doc.get("id") == DEC, doc
    links = json.loads(read(server, f"kb://links/{DEC}"))
    assert links.get("doc_id") == DEC, links


def test_percent_encoded_id_is_decoded(server) -> None:
    doc = json.loads(read(server, "kb://doc/dec%2Fe2e-harness"))
    assert doc.get("id") == DEC, doc
    links = json.loads(read(server, "kb://links/dec%2Fe2e-harness"))
    assert links.get("doc_id") == DEC, links
    assert "not_found" not in json.dumps(links)


def test_graph_percent_encoded_depth_is_decoded(server) -> None:
    # The seam (_is_graph_depth) unquotes before admitting a tail as a depth, so a
    # %33 tail routes to the depth template; the handler must decode it too, or it
    # answers "invalid depth" for a URI the seam classified as valid.
    payload = json.loads(read(server, f"kb://graph/{DEC}/%33"))
    assert "error" not in payload, payload
    assert DEC in payload["doc_ids"], payload


def test_history_and_export_hierarchical_ids(server) -> None:
    hist = json.loads(read(server, f"kb://history/{DEC}"))
    assert "error" not in hist and hist.get("id") == DEC, hist
    assert hist["count"] >= 1, hist
    md = read(server, f"kb://export/{DEC}")  # text/markdown, not JSON
    assert "Could not export" not in md and "层级 id 正文" in md, md


def test_search_query_is_decoded(server) -> None:
    payload = json.loads(read(server, f"kb://search/{JP_QUERY_PCT}"))
    # The decode is the assertion; ranking is not this test's business.
    assert payload.get("query") == JP_QUERY, payload
    assert "%E7%9D" not in json.dumps(payload, ensure_ascii=False)


def test_graph_tail_that_is_a_depth_uses_the_depth_template(server) -> None:
    payload = json.loads(read(server, f"kb://graph/{FLAT}/3"))
    assert "error" not in payload, payload
    assert FLAT in payload["doc_ids"], payload


def test_graph_hierarchical_id_is_not_read_as_a_depth(server) -> None:
    payload = json.loads(read(server, f"kb://graph/{DEC}"))
    assert "error" not in payload, payload
    assert DEC in payload["doc_ids"], payload


def test_graph_hierarchical_id_with_trailing_depth(server) -> None:
    payload = json.loads(read(server, f"kb://graph/{DEC}/3"))
    assert "error" not in payload, payload
    assert DEC in payload["doc_ids"], payload


def test_flat_ids_and_static_resources_are_untouched(server) -> None:
    assert json.loads(read(server, f"kb://links/{FLAT}")).get("doc_id") == FLAT
    assert json.loads(read(server, "kb://list/decision")).get("count") == 2
    assert "error" not in json.loads(read(server, "kb://stats"))


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        # arity already matches: returned byte-identical, never re-encoded
        ("kb://stats", "kb://stats"),
        ("kb://list/spec", "kb://list/spec"),
        ("kb://doc/dec%2Fe2e-harness", "kb://doc/dec%2Fe2e-harness"),
        ("kb://search/" + JP_QUERY, "kb://search/" + JP_QUERY),
        ("kb://graph/e2e-flat", "kb://graph/e2e-flat"),
        ("kb://graph/e2e-flat/3", "kb://graph/e2e-flat/3"),
        # surplus segments collapse into the last parameter, encoded
        ("kb://doc/dec/e2e-harness", "kb://doc/dec%2Fe2e-harness"),
        ("kb://links/dec/e2e-harness", "kb://links/dec%2Fe2e-harness"),
        # graph: an unparseable tail is id, a 1..8 tail is depth
        ("kb://graph/dec/e2e-harness", "kb://graph/dec%2Fe2e-harness"),
        ("kb://graph/dec/e2e-harness/3", "kb://graph/dec%2Fe2e-harness/3"),
        ("kb://graph/dec/e2e-harness/99", "kb://graph/dec%2Fe2e-harness%2F99"),
    ],
)
def test_normalize_resource_uri(uri: str, expected: str) -> None:
    assert _normalize_resource_uri(uri) == expected


def test_get_current_prefers_kb_mcp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_VAULT", raising=False)
    vm = VaultManager(tmp_path)
    vm.create("team")
    assert vm.get_current() == "default"  # registry fallback

    monkeypatch.setenv("KB_MCP_VAULT", "team")
    assert vm.get_current() == "team"  # env wins over the registry
    assert vm.resolve_path().parent.name == "team"  # ...and routes the store

    monkeypatch.setenv("KB_MCP_VAULT", "   ")
    assert vm.get_current() == "default"  # blank is not an override

    monkeypatch.delenv("KB_MCP_VAULT")
    vm.switch("team")
    assert vm.get_current() == "team"  # registry still drives with no env


def test_get_current_vault_name_follows_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The admin-reported name is the same resolution the store opens with."""
    monkeypatch.delenv("KB_MCP_VAULT", raising=False)
    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path))
    vm = VaultManager(tmp_path)
    vm.create("team")
    vm.switch("team")
    assert get_current_vault_name() == "team"  # not a hardcoded default

    monkeypatch.setenv("KB_MCP_VAULT", "other")
    assert get_current_vault_name() == "other"  # env still wins