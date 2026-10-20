# kb-mcp v0.7.0 Release Notes

## What's changed

- **Structured Metadata & Section Extraction**:
  - Added `metadata` JSON column to documents table (migration 0006) and `Document` model.
  - Added heading-level section slicing (`extract_sections()`, `get_section()`, and `--section` option in CLI/MCP) to reduce AI agent token consumption.
- **Chinese / CJK Search Enhancement**:
  - Implemented Bi-gram + character-level CJK tokenization in FTS query builder, solving unspaced CJK term dropouts in SQLite unicode61 tokenizer.
- **Git Diff Context Guard**:
  - Added `kb_diff_check` tool and `kb diff-check` CLI to proactively detect repository code changes and recommend relevant architectural decisions (ADRs), lessons-learned, and API contracts.
- **Knowledge Graph Traversal & Repair**:
  - Added multi-hop BFS path finding (`find_path`) and directional relation traversal (`kb_query_relations`).
  - Added `doctor_fix_hygiene()` for dangling link cleanup and FTS index maintenance.
- **Real-time Markdown File Watcher**:
  - Added `kb watch` CLI and `VaultWatcher` to incrementally synchronize markdown directory file edits, creations, and deletions with SQLite.
- **Smart Markdown Git Conflict Resolver**:
  - Added 3-way/2-way frontmatter and body merge engine for seamless team sync.
- **MCP Server Expansion**:
  - Expanded MCP tool surface to 17 tools with multi-vault routing support.

---

# kb-mcp v0.6.6 Release Notes

## What's changed

- **Quality improvements**:
  - Fixed the mypy type error in `store/search.py` where an `int` limit was
    appended to a `list[str]` parameter list.
  - Aligned CLI exit codes with `architecture.md`: validation errors exit `2`,
    not-found errors exit `3`, duplicate/conflict errors exit `4`, and internal
    errors exit `5`. Vault-specific exceptions now map to the same codes.
  - Removed stray `.bak` files (`md_io.py.bak`, `vault.py.bak`) from the
    repository.
  - Tightened exception handling across `mcp_server.py` and admin routes
    (`routes_docs.py`, `routes_meta.py`): input validation now catches
    `PydanticValidationError`, and business-logic blocks catch the explicit
    `kb_mcp_lite.schema` exception types instead of bare `Exception`.
  - Hardened `tests/test_cli_stub.py::TestServe` against environments where the
    default `KB_MCP_HOME` directory is not writable.

---

# kb-mcp v0.6.5 Release Notes

## What's changed

- **Database robustness**: connections are now created through a single
  factory so pysqlite3 and stdlib sqlite3 are never mixed against one
  WAL-mode database (mixing them could corrupt it — observed as
  "database disk image is malformed"). Row factories follow the
  originating library, migrations tolerate pysqlite3 exceptions, and
  the pre-created `docs_vec` table is recreated when its dimension no
  longer matches the configured embedder.
- **Admin SPA**: the router now waits for external scripts (e.g.
  vis-network, SimpleMDE) to load in order before running a page's
  inline scripts, and skips scripts already loaded — removing
  initialization-order failures on SPA navigation.
- **Graph page**: repeated SPA visits no longer leave the graph blank.
  The page script was made idempotent (IIFE scope) so re-entry rebuilds
  cleanly instead of throwing `Identifier has already been declared`;
  the theme listener is registered exactly once; a loading placeholder
  and fetch error fallback were added.

---

# kb-mcp v0.6.3 Release Notes

## What's changed

- **MCP tools**: added `kb_doctor`, `kb_similar`, and `kb_duplicates`;
  the tool surface is now 15 tools.
- **Search**: explicit `semantic` mode is accepted by `kb_search` when an
  embedder is configured.
- **Resources**: `kb://export/{id}` now returns round-trippable Markdown
  with frontmatter, aliases, and outlinks. `kb://history` and
  `kb://export` now use `{type}/{slug}` so document IDs with a slash can
  be addressed.
- **Help**: built-in help documents are packaged with the distribution and
  served from an allowlist instead of the source-tree `docs/` directory.
- **Prompts**: `new-doc` covers all 9 built-in document types, and
  maintenance/import prompts clearly separate MCP actions from CLI-only
  lifecycle operations.
- **Tests**: MCP tool/resource/prompt inventories are asserted exactly.

---

# kb-mcp v0.6.2 Release Notes

## Overview

v0.6.2 fixes the MCP SDK dependency break: the unbounded `mcp>=1.0`
constraint allowed MCP SDK 2.x to be installed, which removed
`mcp.server.fastmcp` (the FastMCP API this project builds on), causing
`kb serve` to fail with the misleading error
"mcp package not installed; run: pip install mcp".

The dependency is now capped at `mcp>=1.0,<2` so a plain `pip install`
resolves to the latest 1.x SDK (1.29.0).

## What's changed

- **fix**: cap `mcp>=1.0,<2` in `pyproject.toml` to exclude MCP SDK 2.x
- **serve**: `kb serve` works again after installing MCP SDK 1.x

## Installation

```bash
pip install kb-mcp-lite==0.6.2
```

---

# kb-mcp v0.6.1 Release Notes

## Overview

v0.6.1 fixes the version sync issue: v0.5.27 was published with
`__init__.py` still reporting `0.5.25`, causing `kb --version` to show
the wrong version. The version string in `__init__.py` is now synced with
`pyproject.toml` before build.

## What's changed

- **sync**: `__version__` in `__init__.py` now matches `pyproject.toml`
- **style**: ruff format + lint cleanup

## Installation

```bash
pip install kb-mcp-lite==0.6.1
```

---

# kb-mcp v0.5.27 Release Notes

## Overview

v0.5.27 fixes several import round-trip issues that surfaced after the
filename-as-ID change in v0.5.26:

- ``created_at`` and ``updated_at`` from frontmatter are now preserved
  instead of being overwritten with ``now()``.
- Links exported by older versions (using ``make_id``-style targets) are
  automatically resolved to the new filename-based IDs during import.
- Link order in exported YAML is stable (sorted by ``(to_id, rel)``),
  preventing phantom diffs in ``pending_export`` / ``kb vault status``.
- Mypy type error fixed in ``doc_from_frontmatter``.

## What's new in v0.5.26

## Overview

v0.5.26 changes the import ID strategy: when importing Markdown files via
``import_dir`` (CLI ``kb import`` / zip upload / ``kb vault pull``),
the **filename** (relative path minus ``.md``) is now used as the document
ID, instead of auto-generating one from ``type+title``.

This makes imports predictable: the file ``proj/kb-mcp.md`` always gets
ID ``proj/kb-mcp``, regardless of its frontmatter ``title``, and directory
hierarchy maps naturally to ID namespacing.

## What's new in v0.5.26

### Filename-based document IDs on import

- ``import_dir`` now passes ``file_id`` (relative path minus ``.md``) to
  ``doc_from_frontmatter``.
- ``doc_from_frontmatter`` gains a ``file_id`` parameter. ID priority:
  1. frontmatter ``id`` (explicit override)
  2. ``file_id`` (from filename)
  3. ``make_id(type, title)`` (auto-generated, unchanged fallback)
- ``export_dir`` creates parent directories when the source path contains
  subdirectories (e.g. ``sub/nested.md``).

### Backward compatibility

- ``kb_add`` (MCP tool) is **unaffected** — it still uses ``make_id``.
- Export → re-import round-trips are **preserved** via source-based dedup.
- Existing documents with explicit IDs set via ``kb_add`` or the admin UI
  are not touched.

# kb-mcp v0.4.0 Release Notes

## Overview

kb-mcp v0.4.0 introduces **multi-vault** support and **Git-based team sync`,
turning kb-mcp from a personal knowledge base into a shareable team knowledge
base.

## What's new in v0.4.0

### Multi-vault

Vaults are isolated, named SQLite databases. Each vault lives in its own
subdirectory under `KB_MCP_HOME`.

| CLI command | Description |
|---|---|
| `kb vault create <name>` | Create a new vault |
| `kb vault list` | List all vaults |
| `kb vault switch <name>` | Switch the active vault |
| `kb vault current` | Show current vault |
| `kb vault rename <old> <new>` | Rename a vault |
| `kb vault remove <name>` | Remove a vault |
| `kb vault info <name>` | Show vault details |

### Git team sync

Vaults can be shared via any Git remote. The sync uses **Markdown files**
(not binary `.db`) so diffs and merges are text-based and reviewable.

| CLI command | Description |
|---|---|
| `kb vault init-git` | Init Git repo + .gitignore in vault |
| `kb vault commit -m MSG` | Export to Markdown + git commit |
| `kb vault push [remote] [branch]` | Git push |
| `kb vault pull [remote] [branch]` | Git pull + import Markdown |

### MCP vault selection

`kb serve --vault <name>` starts the MCP server for a specific vault.
The `KB_MCP_VAULT` env var also controls the active vault at runtime.

### Migration

On first run, any existing `KB_MCP_HOME/kb.db` is automatically migrated
into a `default/` subdirectory and registered in `vaults.json`. Zero
manual steps required.

### Internal

* New `VaultManager` class in `src/kb_mcp_lite/vault.py`
* `_create_store()` in both `cli.py` and `mcp_server.py` unified to use
  `VaultManager.resolve_path()`
* CLI store caching respects `KB_MCP_VAULT` env var

### Compatibility

* **Backwards compatible** with v0.3.x databases. Existing data is
  migrated to a `default` vault automatically.
* All previous CLI commands and MCP tools work unchanged.
* `kb serve` without `--vault` uses the current active vault (same as
  before).

## Installation

```bash
pip install kb-mcp            # core
pip install 'kb-mcp[vec]'     # adds semantic search
```


# kb-mcp v0.3.0 Release Notes

## Overview

kb-mcp v0.3.0 enhances the **knowledge management experience** with MCP
Resources & Prompts, document version history, and alias support.

## What's new in v0.3.0

### MCP Resources (was 0, now 4)

| Resource URI | Description |
|---|---|
| `kb://doc/{type}/{slug}` | Full document by id |
| `kb://search/{query}` | Search results as JSON |
| `kb://links/{type}/{slug}` | Backlinks and outlinks |
| `kb://doctor` | Health check status |

### MCP Prompts (was 0, now 2)

| Prompt | Description |
|---|---|
| `new-doc` | Guides the agent to create a well-structured document |
| `search-expert` | Guides the agent to search effectively |

### MCP Tools (was 8, now 12)

Four new tools:

| Tool | Description |
|---|---|
| `kb_history(id, limit?)` | View version history |
| `kb_restore(id, version?)` | Restore to a previous version |
| `kb_diff(id, version_a, version_b)` | Field-level diff between versions |
| `kb_restore_deleted(id)` | Restore a soft-deleted document |

### CLI additions

* `kb history ID [--limit N]` — View version history
* `kb restore ID [--version N]` — Restore to a previous version
* `kb diff ID VERSION_A VERSION_B` — Compare two versions
* `kb restore-deleted ID` — Restore a soft-deleted doc
* `kb add --aliases a1,a2,...` — Alternative IDs when creating
* `kb update --aliases a1,a2,...` — Update aliases

### Alias support

Documents can now have alternative IDs (aliases). Searching by alias
works just like searching by primary ID. Aliases are stored in a new
`doc_aliases` table (migration 0005) and persist across sessions.

### Internal

* Store Protocol now includes `document_history()`, `audit_log()`,
  `restore()`, `diff()`, `restore_deleted()`, `resolve_alias()`
* SqliteStore `get()` resolves aliases when direct id lookup fails
* Version snapshots recorded on every create/update/delete in StubStore
  (previously only in SqliteStore)

### Compatibility

* **Backwards compatible** with v0.2.x databases. Migration 0005
  (`doc_aliases`) is applied automatically on first connect.
* All previous CLI commands and MCP tools work unchanged.
* The `kb://doc/{type}/{slug}` URI template replaces the flat
  `kb://doc/{id}` pattern to support document IDs containing slashes.

## Installation

```bash
pip install kb-mcp            # core (lexical + fuzzy)
pip install 'kb-mcp[vec]'     # adds semantic search
```


# kb-mcp v0.2.0 Release Notes

## Overview

kb-mcp v0.2.0 expands the **agent-native knowledge base** with three
new search modes (fuzzy, semantic, and hybrid) and completes the CRUD
surface so agents no longer need to drop into raw SQL to update or
delete a document. The whole release ships in two extras (`pip install
'kb-mcp[vec]'` for semantic search) and remains a single-file SQLite
database.

## What's new in v0.2.0

### Tools (was 4, now 8)

Four new MCP tools, also wired to the CLI:

| Tool | Purpose |
|---|---|
| `kb_list(type?, tags?, limit?, offset?)` | Browse / paginate |
| `kb_update(id, title?, body?, tags?, source?)` | Patch a document |
| `kb_delete(id)` | Soft-delete (idempotent) |
| `kb_unlink(from, to, rel?)` | Remove a typed edge |

The `kb_search` tool gains a `mode` parameter (`lexical` / `fuzzy` /
`semantic` / `hybrid`).

### Fuzzy search (migration 0002)

A second FTS5 index with the `trigram` tokenizer. With `mode=fuzzy`
the agent can match by 3-gram overlap, which catches:

* **Prefix matches** — `faste` finds `fastech-energy`
* **Token-boundary errors** — `fastech energy` finds `fastech-energy`

It does **not** fix edit-distance typos (FTS5 trigrams are
substring-based, not Levenshtein). For that, use the `hybrid` mode
which also surfaces semantic hits.

### Semantic search (migration 0003)

`sqlite-vec` (vec0 virtual table) plus an OpenAI-compatible
`HttpEmbedder` that reads the same `auxiliary.embedding` block
Hermes' vision config already uses. Configure once in
`~/.hermes/config.yaml`:

```yaml
auxiliary:
  embedding:
    base_url: https://api.example.com/v1
    model: text-embedding-3-small
    api_key: sk-...
```

Then `kb search --mode semantic "what is the LLM-friendly API
convention?"` finds semantically related docs even when no exact
token matches. Hybrid mode combines lexical + fuzzy + semantic,
preferring exact matches.

### CLI additions

* `kb update ID --title/--body/--tags/--source`
* `kb delete ID`
* `kb unlink --from ID --to ID [--rel REL]`
* `kb list --include-deleted`
* `kb search --mode {lexical,fuzzy,semantic,hybrid}`
* `kb embed` (status) / `kb embed --rebuild` (re-embed everything)
* `kb prune --older-than 30d` (hard-delete past grace period)

### Internal

* `SqliteStore` now accepts an `embedder=` parameter (defaults to
  `make_embedder()` which auto-detects from `auxiliary.embedding`).
* Soft-delete no longer drifts the FTS index (the
  external-content projection can't represent deletion; the
  search layer filters `deleted_at IS NULL` instead).
* `kb doctor` reports `active_fts_rows` (FTS rows joined to
  non-deleted docs) rather than the misleading raw count.

## Installation

```bash
pip install kb-mcp            # core (lexical + fuzzy)
pip install 'kb-mcp[vec]'     # adds semantic search (sqlite-vec + pysqlite3)
```

## Compatibility

* **Backwards compatible** with v0.1.0 databases. The two new
  migrations (`0002_trgm`, `0003_vec`) are applied on first connect.
  `0003_vec` is best-effort: it skips silently if `pysqlite3` /
  `sqlite-vec` aren't installed, and the rest of kb-mcp keeps
  working.
* The Store Protocol is unchanged. `mode` is a new keyword
  argument on `search()`; existing callers default to
  `mode="lexical"`, which is identical to v0.1 behaviour.

## Known limitations

* FTS5 trigrams can't fix insert-letter typos (`sqlitte` vs
  `sqlite`). This is a fundamental property of the tokenizer, not
  a bug. For real edit-distance fuzzy matching, use the `hybrid`
  mode (which also calls the embedding API).
* `mode='semantic'` requires the embedder to be configured. If
  not, the call raises `ValidationError` with a clear message
  pointing to `auxiliary.embedding`.
* macOS arm64 requires `pysqlite3` (no `pysqlite3-binary` wheel
  for that platform). The `vec` extra pulls it in automatically.

---

# kb-mcp v0.1.0 Release Notes

## Overview

kb-mcp is an **agent-native knowledge base** built on SQLite + FTS5, exposed as an MCP (Model Context Protocol) server. It lets AI agents store, search, and link structured Markdown documents with full-text search, soft-delete, and typed relationships.

## What's in v0.1.0

### Core Features

- **Document schema** — 6 built-in types (`project`, `decision`, `lesson`, `glossary`, `person`, `faq`) with extensible type registry
- **SQLite + FTS5 backend** — Full-text search with BM25 ranking, snippets, and soft-delete
- **MCP server** — 4 tools over stdio: `kb_search`, `kb_get`, `kb_add`, `kb_link`
- **CLI** — `kb init`, `kb add`, `kb get`, `kb search`, `kb list`, `kb link`, `kb doctor`, `kb import`, `kb export`
- **Markdown I/O** — Import/export documents with YAML frontmatter
- **Migration runner** — Idempotent DDL with versioning

### Technical Highlights

- **Store Protocol** — Abstract interface enabling multiple backends (SQLite, in-memory StubStore)
- **Soft delete + prune** — Documents are marked deleted, not removed; `prune` hard-deletes after grace period
- **Idempotent links** — Typed edges between documents, deduplicated by `(from_id, to_id, rel)`
- **Source-based upsert** — Re-importing the same file updates rather than duplicates
- **100% test coverage** — 136 tests across unit, integration, and E2E (real SQLite, subprocess MCP server)
- **Type-safe** — Full mypy coverage with zero errors

## Commits since scaffold (12)

| Commit | Description |
|--------|-------------|
| `67d75be` | Scaffold project |
| `6b28160` | README, requirements, quickstart, cli-reference |
| `d153593` | 4-wave implementation plan |
| `efc2f69` | Schema + Store Protocol + DDL + architecture |
| `da70d9d` | SqliteStore + migration runner + 38 tests |
| `2192eb5` | Subagent output landed |
| `a22c14f` | MCP server with 4 tools + E2E tests |
| `32672a7` | Address Wave 1 review findings |
| `42a9d3d` | CONTRIBUTING + CoC + client examples |
| `0b446ff` | CI workflows + pyproject fixes |
| `6dcaeaa` | Code review Wave 2A+3 |
| `3640f86` | Address Wave 2A+3 review findings |
| `9839a69` | Wire md_io into CLI + CJK slugify |

## Installation

```bash
pip install kb-mcp
```

Or with uv:
```bash
uv tool install kb-mcp
```

## Quick Start

```bash
# Initialize database
kb init

# Add a document
kb add --type project --title "kb-mcp" --body "Agent-native knowledge base"

# Search
kb search "knowledge base"

# Start MCP server
kb serve
```

## PyPI Trusted Publisher Setup

Before the first release, configure **OIDC trusted publishing** on PyPI and TestPyPI:

1. **PyPI**: Go to [pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing)
   - Add a new pending publisher:
     - **PyPI Project Name**: `kb-mcp`
     - **Owner**: `zhangbei`
     - **Repository name**: `kb-mcp`
     - **Workflow name**: `publish.yml`
     - **Environment name**: `pypi`

2. **TestPyPI**: Go to [test.pypi.org/manage/account/publishing](https://test.pypi.org/manage/account/publishing)
   - Add a new pending publisher:
     - **PyPI Project Name**: `kb-mcp`
     - **Owner**: `zhangbei`
     - **Repository name**: `kb-mcp`
     - **Workflow name**: `publish.yml`
     - **Environment name**: `testpypi`

The `publish.yml` workflow automatically detects prerelease tags (`v0.1.0rc1`, `v0.1.0a1`) and routes them to TestPyPI; stable tags (`v0.1.0`) go to PyPI.

## Recommended Release Flow

1. Tag `v0.1.0rc1` → TestPyPI → verify OIDC flow works
2. Tag `v0.1.0` → PyPI → stable release

---

**Full documentation**: [docs/architecture.md](docs/architecture.md)  
**Contributing**: [CONTRIBUTING.md](CONTRIBUTING.md)  
**License**: MIT
