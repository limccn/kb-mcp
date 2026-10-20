"""Contract tests for the MCP server's public surface."""

from __future__ import annotations

from kb_mcp_lite.mcp_server import KbSearchInput, _DOC_TEMPLATES, _HELP_DOCS
from kb_mcp_lite.schema import default_registry


def test_semantic_mode_is_accepted_by_input_model() -> None:
    """The MCP input model allows explicit semantic search."""
    inp = KbSearchInput(query="kb", mode="semantic")
    assert inp.mode == "semantic"


def test_every_builtin_type_has_a_new_doc_template() -> None:
    """Prompt templates cannot drift from the schema type registry."""
    builtin_types = set(default_registry.known_types())
    assert builtin_types <= set(_DOC_TEMPLATES)


def test_help_whitelist_contains_no_path_separators() -> None:
    """Help resource names must stay safe to join as filenames."""
    assert all("/" not in name and "\\" not in name for name in _HELP_DOCS)
