"""Command-line interface."""

import os
import sys
import json
import functools
from pathlib import Path
from typing import cast, Any, TypeVar
from collections.abc import Callable

import click
from pydantic import ValidationError

from kb_mcp_lite import __version__
from kb_mcp_lite.config import load_config as get_config
from kb_mcp_lite.md_io import import_dir, export_dir
from kb_mcp_lite.ingest import (
    ConverterNotFoundError,
    ConverterRunError,
    ConverterValidationError,
    run_ingest,
)
from kb_mcp_lite.schema import (
    Document,
    KbMcpError,
    NotFoundError,
    DuplicateError,
)
from kb_mcp_lite.concurrency import ResourceBusyError
from kb_mcp_lite.vault import (
    VaultAlreadyExistsError,
    VaultManager,
    VaultNotFoundError,
)


F = TypeVar("F", bound=Callable[..., Any])

# Exit codes — match the expected values from tests.
EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_INTERNAL = 5
EXIT_USAGE = 64


# ---- helpers ----------------------------------------------------------------


def _handle_errors(func: F) -> F:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ValidationError as e:
            click.echo(f"Validation error: {e}", err=True)
            sys.exit(EXIT_VALIDATION)
        except NotFoundError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(EXIT_NOT_FOUND)
        except DuplicateError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(EXIT_CONFLICT)
        except VaultAlreadyExistsError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(EXIT_CONFLICT)
        except VaultNotFoundError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(EXIT_NOT_FOUND)
        except ResourceBusyError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(EXIT_CONFLICT)
        except KbMcpError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(EXIT_INTERNAL)
        except Exception as e:
            click.echo(f"Unexpected error: {type(e).__name__}: {e}", err=True)
            if os.environ.get("KB_DEBUG"):
                raise
            sys.exit(EXIT_INTERNAL)

    return wrapper  # type: ignore


def _get_store(ctx: click.Context) -> Any:
    return ctx.obj["store"]


class _NullLock:
    """No-op stand-in for WriteLock when --strict-lock is not enabled.

    Implements the context-manager protocol so call sites do not need to
    branch on whether strict locking is active.
    """

    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _write_lock(ctx: click.Context):
    """Return a context manager that takes the store write lock iff enabled.

    When ``--strict-lock`` (or ``KB_MCP_STRICT_LOCK``) is set the returned
    object is a real :class:`WriteLock` that blocks until the sidecar file
    is free. Otherwise it is a no-op stand-in that always succeeds.
    """
    if ctx.obj.get("strict_lock"):
        return _get_store(ctx).write_lock
    return _NullLock()


def _json_option(func: F) -> F:
    return click.option("--json", "as_json", is_flag=True, help="Output results as JSON.")(func)


# ---- main cli -----------------------------------------------------------------


@click.group(name="kb", help=f"kb: Agent-native knowledge base. v{__version__}")
@click.version_option(__version__)
@click.option("--vault", help="Use a specific vault by name or path.")
@click.option(
    "--strict-lock/--no-strict-lock",
    default=None,
    help=(
        "Enable cross-process write locking (fcntl/flock). "
        "Defaults to $KB_MCP_STRICT_LOCK or False."
    ),
)
@click.option(
    "--lock-timeout",
    type=float,
    default=None,
    help="Write-lock acquisition timeout in seconds (default: 10.0, or $KB_MCP_LOCK_TIMEOUT).",
)
@click.pass_context
def cli(
    ctx: click.Context,
    vault: str | None,
    strict_lock: bool | None,
    lock_timeout: float | None,
) -> None:
    config = get_config()
    vault_manager = VaultManager()
    selected_vault = vault or vault_manager.get_current()
    ctx.ensure_object(dict)

    # Resolve strict_lock from CLI flag -> env var -> default False
    if strict_lock is None:
        strict_lock = os.environ.get("KB_MCP_STRICT_LOCK", "").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    if lock_timeout is None:
        try:
            lock_timeout = float(os.environ.get("KB_MCP_LOCK_TIMEOUT", "10.0"))
        except ValueError:
            lock_timeout = 10.0

    # Use injected store from test if present; otherwise create a new one
    if "store" not in ctx.obj:
        from kb_mcp_lite.store import SqliteStore

        db_path = vault_manager.resolve_path(selected_vault)
        ctx.obj["store"] = SqliteStore(
            db_path,
            strict_lock=strict_lock,
            lock_timeout=lock_timeout,
        )
    # Use injected config/vault_manager if present; otherwise set them up
    if "config" not in ctx.obj:
        ctx.obj["config"] = config
    if "vault_manager" not in ctx.obj:
        ctx.obj["vault_manager"] = vault_manager
    ctx.obj["strict_lock"] = strict_lock
    ctx.obj["lock_timeout"] = lock_timeout


def _emit_json(obj: Any) -> None:
    click.echo(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---- kb init -----------------------------------------------------------------


@cli.command()
@click.pass_context
@_handle_errors
def init(ctx: click.Context) -> None:
    """Initialize a new knowledge base."""
    store = _get_store(ctx)
    store.init()
    click.echo("Initialized kb.")


# ---- kb add ------------------------------------------------------------------


@cli.command()
@click.option("--type", "doc_type", required=True, help="Document type.")
@click.option("--title", required=True, help="Document title.")
@click.option("--tags", help="Comma-separated list of tags.")
@click.option("--body", help="Document body (Markdown).")
@_json_option
@click.pass_context
@_handle_errors
def add(
    ctx: click.Context,
    doc_type: str,
    title: str,
    tags: str | None,
    body: str | None,
    as_json: bool,
) -> None:
    """Add a new document."""
    store = _get_store(ctx)
    tag_list = tags.split(",") if tags else []
    doc = Document(
        id="",  # Auto-generated
        type=doc_type,
        title=title,
        tags=tag_list,
        body=body or "",
    )
    with _write_lock(ctx):
        doc_id = store.add(doc)
    if as_json:
        _emit_json({"id": doc_id})
    else:
        click.echo(f"Added document: {doc_id}")


# ---- kb get ------------------------------------------------------------------


@cli.command()
@click.argument("doc_id")
@click.option("--section", help="Optional heading/section to extract.")
@_json_option
@click.pass_context
@_handle_errors
def get(ctx: click.Context, doc_id: str, section: str | None, as_json: bool) -> None:
    """Get a document by ID."""
    store = _get_store(ctx)
    doc = store.get(doc_id)
    if as_json:
        dumped = doc.model_dump(mode="json")
        if section:
            sec_content = doc.get_section(section)
            if sec_content is None:
                dumped["section_found"] = False
                dumped["available_sections"] = [s for s in doc.extract_sections().keys() if s]
            else:
                dumped["section_found"] = True
                dumped["section_name"] = section
                dumped["body"] = sec_content
        _emit_json(dumped)
    else:
        click.echo(f"ID: {doc.id}")
        click.echo(f"Type: {doc.type}")
        click.echo(f"Title: {doc.title}")
        click.echo(f"Tags: {', '.join(doc.tags) if doc.tags else '(none)'}")
        if doc.metadata:
            click.echo(f"Metadata: {json.dumps(doc.metadata, ensure_ascii=False)}")
        click.echo(f"Created: {doc.created_at.isoformat()}")
        click.echo(f"Updated: {doc.updated_at.isoformat()}")
        click.echo("")
        if section:
            sec_content = doc.get_section(section)
            if sec_content is None:
                click.echo(f"Section {section!r} not found.")
            else:
                click.echo(sec_content)
        else:
            click.echo(doc.body)


# ---- kb update ---------------------------------------------------------------


@cli.command()
@click.argument("doc_id")
@click.option("--title", help="New title.")
@click.option("--tags", help="New comma-separated tags list.")
@click.option("--body", help="New body.")
@_json_option
@click.pass_context
@_handle_errors
def update(
    ctx: click.Context,
    doc_id: str,
    title: str | None,
    tags: str | None,
    body: str | None,
    as_json: bool,
) -> None:
    """Update a document."""
    store = _get_store(ctx)
    updates: dict[str, Any] = {}
    if title:
        updates["title"] = title
    if tags is not None:
        updates["tags"] = tags.split(",") if tags else []
    if body is not None:
        updates["body"] = body
    if not updates:
        click.echo("No updates specified.", err=True)
        sys.exit(1)
    with _write_lock(ctx):
        updated = store.update(doc_id, **updates)
    if as_json:
        _emit_json(updated.model_dump(mode="json"))
    else:
        click.echo(f"Updated {doc_id}")


# ---- kb delete ----------------------------------------------------------------


@cli.command()
@click.argument("doc_id")
@_json_option
@click.pass_context
@_handle_errors
def delete(ctx: click.Context, doc_id: str, as_json: bool) -> None:
    """Soft-delete a document."""
    store = _get_store(ctx)
    with _write_lock(ctx):
        store.delete(doc_id)
    if as_json:
        _emit_json({"deleted": doc_id})
    else:
        click.echo(f"Deleted {doc_id}")


# ---- kb restore ---------------------------------------------------------------


@cli.command()
@click.argument("doc_id")
@click.option("--version", type=int, help="Restore to a specific version number.")
@_json_option
@click.pass_context
@_handle_errors
def restore(ctx: click.Context, doc_id: str, version: int | None, as_json: bool) -> None:
    """Restore a soft-deleted document or restore to a previous version."""
    store = _get_store(ctx)
    with _write_lock(ctx):
        if version is not None:
            restored = store.restore_version(doc_id, version)
        else:
            restored = store.restore_deleted(doc_id)
    if as_json:
        _emit_json(restored.model_dump(mode="json"))
    else:
        click.echo(f"Restored {doc_id}")


# ---- kb search ---------------------------------------------------------------


@cli.command()
@click.argument("query")
@click.option("--type", "doc_type", help="Filter by document type.")
@click.option("--tags", multiple=True, help="Filter by tags (may be used multiple times).")
@click.option("--fuzzy", is_flag=True, help="Use fuzzy trigram search.")
@click.option(
    "--mode",
    type=click.Choice(["lexical", "fuzzy", "semantic", "hybrid", "rrf"]),
    default=None,
    help="Search scoring mode (default lexical, or hybrid if preferred).",
)
@click.option("--rerank", is_flag=True, help="Apply Cross-Encoder reranking to results.")
@click.option("--limit", default=20, type=click.IntRange(1, 100), show_default=True)
@_json_option
@click.pass_context
@_handle_errors
def search(
    ctx: click.Context,
    query: str,
    doc_type: str | None,
    tags: tuple[str, ...],
    fuzzy: bool,
    mode: str | None,
    rerank: bool,
    limit: int,
    as_json: bool,
) -> None:
    """Search the knowledge base."""
    store = _get_store(ctx)
    tag_list = list(tags) if tags else None
    search_mode = mode or ("fuzzy" if fuzzy else "lexical")
    results = store.search(
        query,
        type=doc_type,
        tags=tag_list,
        mode=search_mode,
        limit=limit,
        rerank=rerank,
    )
    if as_json:
        _emit_json(
            [
                {
                    "doc": hit.doc.model_dump(mode="json"),
                    "snippet": hit.snippet,
                    "score": hit.score,
                }
                for hit in results
            ]
        )
    else:
        if not results:
            click.echo("(no results)")
            return
        for i, hit in enumerate(results, 1):
            click.echo(f"{i}. {hit.doc.id}  [{hit.doc.type}]  {hit.doc.title}")
            click.echo(f"   {hit.snippet}")
            click.echo()


# ---- kb list -----------------------------------------------------------------


@cli.command("list")
@click.option(
    "--type",
    "doc_type",
    help="Filter by document type (e.g. decision, lesson).",
)
@click.option(
    "--tags",
    multiple=True,
    help="Filter by tags (all specified tags must be present). May be used multiple times.",
)
@click.option(
    "--project",
    help="Filter documents linked to this project ID (shortcut for --link-to <proj/id>).",
)
@click.option(
    "--link-to",
    help="Filter documents that link to this document ID.",
)
@click.option(
    "--link-from",
    help="Filter documents that are linked from this document ID.",
)
@click.option(
    "--limit",
    default=100,
    show_default=True,
    type=click.IntRange(1, 1000),
    help="Return at most this many results.",
)
@click.option(
    "--offset",
    default=0,
    show_default=True,
    type=click.IntRange(0),
    help="Skip this many results before returning (pagination).",
)
@click.option(
    "--include-deleted",
    is_flag=True,
    help="Include soft-deleted documents (default: hide them).",
)
@_json_option
@click.pass_context
@_handle_errors
def list_cmd(
    ctx: click.Context,
    doc_type: str | None,
    tags: tuple[str, ...],
    project: str | None,
    link_to: str | None,
    link_from: str | None,
    limit: int,
    offset: int,
    include_deleted: bool,
    as_json: bool,
) -> None:
    """List documents, sorted by ``updated_at`` DESC."""
    store = _get_store(ctx)
    tag_list = list(tags) if tags else None

    # Handle --project shortcut
    if project:
        if not project.startswith("proj/"):
            project = f"proj/{project}"
        link_to = project

    docs = store.list(
        type=doc_type,
        tags=tag_list,
        link_to=link_to,
        link_from=link_from,
        limit=limit,
        offset=offset,
        include_deleted=include_deleted,
    )
    if as_json:
        _emit_json([d.model_dump(mode="json") for d in docs])
    else:
        if not docs:
            click.echo("(no documents)")
            return
        for d in docs:
            click.echo(f"{d.id}  [{d.type}]  {d.title}  ({d.updated_at.isoformat()})")


# ---- kb link -----------------------------------------------------------------


@cli.command()
@click.option("--from", "from_id", required=True, help="Source document id.")
@click.option("--to", "to_id", required=True, help="Target document id.")
@click.option(
    "--rel",
    default="relates-to",
    show_default=True,
    help="Relation type (default: relates-to).",
)
@_json_option
@click.pass_context
@_handle_errors
def link(
    ctx: click.Context,
    from_id: str,
    to_id: str,
    rel: str,
    as_json: bool,
) -> None:
    """Create a typed link between two documents."""
    store = _get_store(ctx)
    with _write_lock(ctx):
        store.link(from_id, to_id, rel)
    if as_json:
        _emit_json({"from": from_id, "to": to_id, "rel": rel})
    else:
        click.echo(f"Linked {from_id} -> {to_id} ({rel})")


# ---- kb unlink ---------------------------------------------------------------


@cli.command()
@click.option("--from", "from_id", required=True, help="Source document id.")
@click.option("--to", "to_id", required=True, help="Target document id.")
@click.option("--rel", help="Only remove links with this specific relation.")
@_json_option
@click.pass_context
@_handle_errors
def unlink(
    ctx: click.Context,
    from_id: str,
    to_id: str,
    rel: str | None,
    as_json: bool,
) -> None:
    """Remove a link between two documents."""
    store = _get_store(ctx)
    with _write_lock(ctx):
        store.unlink(from_id, to_id, rel)
    if as_json:
        _emit_json({"removed": f"{from_id} -> {to_id}"})
    else:
        click.echo(f"Removed link {from_id} -> {to_id}")


# ---- kb links ----------------------------------------------------------------


@cli.command()
@click.argument("doc_id")
@_json_option
@click.pass_context
@_handle_errors
def links(ctx: click.Context, doc_id: str, as_json: bool) -> None:
    """Show all incoming and outgoing links for a document."""
    store = _get_store(ctx)
    outgoing = store.outgoing_links(doc_id)
    incoming = store.incoming_links(doc_id)
    if as_json:
        _emit_json(
            {
                "outgoing": [
                    {"to": link.to_id, "rel": link.rel, "created_at": link.created_at}
                    for link in outgoing
                ],
                "incoming": [
                    {"from": link.from_id, "rel": link.rel, "created_at": link.created_at}
                    for link in incoming
                ],
            }
        )
    else:
        click.echo(f"Links for {doc_id}:")
        click.echo("")
        click.echo("Outgoing:")
        if not outgoing:
            click.echo("  (none)")
        else:
            for link in outgoing:
                click.echo(f"  -> {link.to_id}  ({link.rel})")
        click.echo("")
        click.echo("Incoming:")
        if not incoming:
            click.echo("  (none)")
        else:
            for link in incoming:
                click.echo(f"  <- {link.from_id}  ({link.rel})")


# ---- kb rel (v0.8 特性 #6) ------------------------------------------------


@cli.group(name="rel", invoke_without_command=True)
@click.pass_context
def rel_group(ctx: click.Context) -> None:
    """Manage the typed relation vocabulary."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@rel_group.command("list")
@_json_option
@click.pass_context
@_handle_errors
def rel_list(ctx: click.Context, as_json: bool) -> None:
    """List all standard relation types."""
    from kb_mcp_lite.relations import STANDARD_RELATIONS

    if as_json:
        _emit_json(
            [
                {
                    "name": s.name,
                    "forward_label": s.forward_label,
                    "is_influence": s.is_influence,
                    "is_supersession": s.is_supersession,
                    "description": s.description,
                }
                for s in STANDARD_RELATIONS.values()
            ]
        )
    else:
        for spec in STANDARD_RELATIONS.values():
            flags = []
            if spec.is_influence:
                flags.append("influence")
            if spec.is_supersession:
                flags.append("supersession")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            click.echo(f"  {spec.name:20s} {spec.forward_label}{flag_str}")


@rel_group.command("show")
@click.argument("rel_name")
@_json_option
@click.pass_context
@_handle_errors
def rel_show(ctx: click.Context, rel_name: str, as_json: bool) -> None:
    """Show details for a specific relation type."""
    from kb_mcp_lite.relations import get_relation_spec

    spec = get_relation_spec(rel_name)
    if spec is None:
        raise click.ClickException(f"unknown relation: {rel_name!r}")
    if as_json:
        _emit_json(
            {
                "name": spec.name,
                "forward_label": spec.forward_label,
                "backward_label": spec.backward_label,
                "default_direction": spec.default_direction,
                "traversal_cost": spec.traversal_cost,
                "is_influence": spec.is_influence,
                "is_supersession": spec.is_supersession,
                "description": spec.description,
            }
        )
    else:
        click.echo(f"Relation: {spec.name}")
        click.echo(f"  Forward:  {spec.forward_label}")
        click.echo(f"  Backward: {spec.backward_label}")
        click.echo(f"  Direction: {spec.default_direction}")
        click.echo(f"  Traversal cost: {spec.traversal_cost}")
        click.echo(f"  Influence: {spec.is_influence}")
        click.echo(f"  Supersession: {spec.is_supersession}")
        click.echo(f"  Description: {spec.description}")


# ---- kb impact / kb chain (v0.8 特性 #6) -----------------------------------


@cli.command()
@click.argument("doc_id")
@click.option("--max-depth", default=3, type=int, help="Max traversal hops.")
@click.option("--max-results", default=50, type=int, help="Max results to return.")
@_json_option
@click.pass_context
@_handle_errors
def impact(
    ctx: click.Context,
    doc_id: str,
    max_depth: int,
    max_results: int,
    as_json: bool,
) -> None:
    """Impact analysis: find documents influenced by the given document."""
    store = _get_store(ctx)
    from kb_mcp_lite.relations import ImpactAnalyzer

    analyzer = ImpactAnalyzer(store)
    nodes = analyzer.analyze(root_id=doc_id, max_depth=max_depth, max_results=max_results)
    if as_json:
        _emit_json(
            [
                {
                    "id": n.doc.id,
                    "title": n.doc.title,
                    "type": n.doc.type,
                    "distance": n.distance,
                    "via": n.via,
                    "rel": n.rel,
                    "path": n.path,
                }
                for n in nodes
            ]
        )
    else:
        if not nodes:
            click.echo(f"No impact found for {doc_id}")
            return
        click.echo(f"Impact from {doc_id} ({len(nodes)} documents):")
        for n in nodes:
            click.echo(
                f"  {'  ' * (n.distance - 1)}[d{n.distance}] {n.doc.id} ({n.rel} via {n.via})"
            )


@cli.command()
@click.argument("decision_id")
@_json_option
@click.pass_context
@_handle_errors
def chain(ctx: click.Context, decision_id: str, as_json: bool) -> None:
    """Trace the supersession chain for a decision document."""
    store = _get_store(ctx)
    from kb_mcp_lite.relations import supersession_chain

    chain_result = supersession_chain(store, decision_id)
    if as_json:
        _emit_json({"decision_id": decision_id, "chain": chain_result, "count": len(chain_result)})
    else:
        if len(chain_result) == 1:
            click.echo(f"{decision_id} (no supersession links)")
        else:
            click.echo(" -> ".join(chain_result))


# ---- kb history ---------------------------------------------------------------


@cli.command()
@click.argument("doc_id")
@_json_option
@click.pass_context
@_handle_errors
def history(ctx: click.Context, doc_id: str, as_json: bool) -> None:
    """Show version history for a document."""
    store = _get_store(ctx)
    versions = store.get_versions(doc_id)
    if as_json:
        _emit_json([v if isinstance(v, dict) else v.model_dump(mode="json") for v in versions])
    else:
        if not versions:
            click.echo("(no history)")
            return
        for v in versions:
            if isinstance(v, dict):
                version_id = v.get("version_id", "?")
                created_at = v.get("created_at", "")
                message = v.get("note", "") or v.get("message", "")
            else:
                version_id = v.version
                created_at = v.created_at
                message = getattr(v, "message", "")
            click.echo(f"Version {version_id}: {created_at}")
            if message:
                click.echo(f"  {message}")
            click.echo()


# ---- kb diff -----------------------------------------------------------------


@cli.command()
@click.argument("doc_id")
@click.option("--v1", type=int, required=True, help="First version number.")
@click.option("--v2", type=int, required=True, help="Second version number.")
@_json_option
@click.pass_context
@_handle_errors
def diff(ctx: click.Context, doc_id: str, v1: int, v2: int, as_json: bool) -> None:
    """Show field-level diff between two versions of a document."""
    store = _get_store(ctx)
    diff_result = store.diff_versions(doc_id, v1, v2)
    if as_json:
        _emit_json(diff_result)
    else:
        for field, changes in diff_result.items():
            click.echo(f"{field}:")
            click.echo(f"  v{v1}: {changes['old']}")
            click.echo(f"  v{v2}: {changes['new']}")
            click.echo()


# ---- kb import ---------------------------------------------------------------


@cli.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option(
    "--dry-run", is_flag=True, help="Only show what would be imported, don't write anything."
)
@_json_option
@click.pass_context
@_handle_errors
def import_cmd(ctx: click.Context, directory: str, dry_run: bool, as_json: bool) -> None:
    """Import Markdown files from a directory into the knowledge base."""
    store = _get_store(ctx)
    report = import_dir(store, Path(directory), dry_run=dry_run)
    if as_json:
        _emit_json(report.model_dump(mode="json"))
    else:
        click.echo(
            f"Imported {report.inserted + report.updated} files: {report.inserted} inserted, {report.updated} updated"
        )
        if report.skipped > 0:
            click.echo(f"Skipped {report.skipped} files")
        if report.errors:
            click.echo("\nErrors:")
            for err in report.errors:
                click.echo(f"  - {err}")


# ---- kb ingest ---------------------------------------------------------------


@cli.command()
@click.argument("directory", required=False,
                type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option(
    "--file",
    "-f",
    "file_path",
    default=None,
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    help="Convert this one file instead of a folder; its folder becomes the root.",
)
@click.option(
    "--ziped",
    "-z",
    "zip_path",
    default=None,
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    help="Convert the members of this .zip archive instead of a folder; the "
         "archive's folder becomes the root. The archive is unpacked by the "
         "converter (one level; nested archives are skipped).",
)
@click.option(
    "--out",
    "out_dir",
    required=True,
    type=click.Path(file_okay=False),
    help="Directory for the import-ready Markdown tree (written by the converter).",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help="Also import the converted tree into this vault (created if missing).",
)
@click.option(
    "--type",
    "doc_type",
    default=None,
    help="frontmatter type for generated documents (converter default: spec).",
)
@click.option(
    "--incremental/--no-incremental",
    "incremental",
    default=None,
    help=(
        "Forward incremental mode to the converter (on by default: unchanged files "
        "are skipped, outputs of vanished sources are pruned). --no-incremental "
        "forces a full reconversion; no flag is forwarded unless given."
    ),
)
@click.option(
    "--converter",
    "converter",
    default=None,
    help=(
        "Conversion command (an executable or .cmd). Default: $KB_INGEST_CONVERTER, "
        "then the repo's ingestion-rt\\doc-convert.exe or ingestion venv."
    ),
)
@_json_option
@click.pass_context
@_handle_errors
def ingest(
    ctx: click.Context,
    directory: str | None,
    out_dir: str,
    file_path: str | None,
    zip_path: str | None,
    vault_name: str | None,
    doc_type: str | None,
    incremental: bool | None,
    converter: str | None,
    as_json: bool,
) -> None:
    """Convert a folder -- or one file, or one .zip archive -- to Markdown.

    Give exactly one of DIRECTORY, --file or --ziped. The tree mirrors the
    input's root: for the single-input forms that is the named file's own
    folder, so `kb ingest -f /docs/a.docx` and `kb ingest /docs` (a folder
    holding nothing else) produce the same tree, manifest included.

    Conversion is delegated to the external ingestion converter (an executable
    or .cmd: --converter, then $KB_INGEST_CONVERTER, then the dev checkout's
    converter) and is incremental by default -- unchanged files are skipped
    and outputs of vanished sources are pruned; --no-incremental forces a full
    reconversion. With --vault the tree is then imported into that vault
    (created on first use). Exit code 1 means a file failed to convert or an
    import error was recorded -- the same "command ok, content not" split as
    the sibling ingestion tools.
    """
    given = [value for value in (directory, file_path, zip_path) if value]
    if len(given) != 1:
        click.echo(
            "Validation error: give exactly one of DIRECTORY, --file, --ziped "
            f"(got {len(given)})", err=True)
        sys.exit(EXIT_VALIDATION)
    target = given[0]
    if directory is not None:
        mode = "dir"
    elif file_path is not None:
        mode = "file"
    else:
        mode = "zip"
        if Path(target).suffix.lower() != ".zip":
            click.echo(f"Validation error: --ziped takes a .zip archive: {target}",
                       err=True)
            sys.exit(EXIT_VALIDATION)

    try:
        result = run_ingest(
            Path(target),
            Path(out_dir),
            vault_name=vault_name,
            doc_type=doc_type,
            incremental=incremental,
            converter=converter,
            vault_manager=ctx.obj["vault_manager"],
            mode=mode,
        )
    except ConverterValidationError as e:
        click.echo(f"Validation error: {e}", err=True)
        sys.exit(EXIT_VALIDATION)
    except ConverterNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_NOT_FOUND)
    except ConverterRunError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_INTERNAL)

    if as_json:
        _emit_json(result.to_dict())
    else:
        click.echo(
            f"{result.converted} converted, {result.failed} failed, "
            f"{len(result.skipped)} skipped, {result.unchanged} unchanged, "
            f"{result.pruned} pruned"
        )
        for line in result.notable_lines:
            click.echo(line)
        click.echo(f"tree: {result.out}")
        if result.enrich is not None:
            click.echo(result.enrich.summary())
        if result.import_summary is not None:
            imp = result.import_summary
            click.echo(f"vault: {imp.vault} ({'created' if imp.created else 'exists'})")
            click.echo(
                f"Imported {imp.inserted + imp.updated} files: "
                f"{imp.inserted} inserted, {imp.updated} updated"
            )
            if imp.skipped > 0:
                click.echo(f"Skipped {imp.skipped} files")
            if imp.errors:
                click.echo("Errors:")
                for err in imp.errors:
                    click.echo(f"  - {err}")
    if result.returncode != 0:
        sys.exit(result.returncode)


# ---- kb export ---------------------------------------------------------------


@cli.command()
@click.argument("directory", type=click.Path(file_okay=False, dir_okay=True))
@click.option("--force", is_flag=True, help="Overwrite existing files.")
@_json_option
@click.pass_context
@_handle_errors
def export(ctx: click.Context, directory: str, force: bool, as_json: bool) -> None:
    """Export all documents as Markdown files to a directory."""
    store = _get_store(ctx)
    export_dir(store, Path(directory), force=force)
    if as_json:
        _emit_json({"exported_to": directory})
    else:
        click.echo(f"Exported all documents to {directory}")


# ---- kb embed ---------------------------------------------------------------


@cli.group(invoke_without_command=True)
@click.option(
    "--rebuild",
    is_flag=True,
    help="Recompute embeddings for all active documents (use after changing models).",
)
@_json_option
@click.pass_context
@_handle_errors
def embed(ctx: click.Context, rebuild: bool, as_json: bool) -> None:
    """Manage semantic-search embeddings.

    Without a subcommand, prints the embedder status. With ``--rebuild``,
    recomputes the embedding for every active document. ``status`` shows
    the embedding-queue breakdown; ``retry`` re-runs failed jobs.
    """
    if ctx.invoked_subcommand is not None:
        return

    store = _get_store(ctx)
    emb = getattr(store, "_embedder", None)
    enabled = bool(emb and getattr(emb, "enabled", False))
    dim = getattr(emb, "dim", 0) if emb else 0

    if rebuild:
        if not enabled:
            click.echo("no embedder configured; set embedding in config file", err=True)
            sys.exit(1)
        n = store.reindex_embeddings(
            progress_callback=lambda i, t: click.echo(f"  [{i}/{t}] embedding...", err=True)
        )
        report = getattr(store, "last_reindex_report", {}) or {}
        dim = report.get("dim") or dim
        failed = report.get("failed", 0)
        if as_json:
            _emit_json(
                {
                    "ok": True,
                    "reindexed": n,
                    "failed": failed,
                    "dim": dim,
                    "total": report.get("total", n + failed),
                }
            )
        else:
            msg = f"re-embedded {n} document(s) (dim={dim})"
            if failed:
                msg += f", {failed} failed"
            click.echo(msg)
        return

    # Bare `kb embed` — status summary (legacy behaviour).
    status = store.embedding_status()
    if as_json:
        _emit_json(status)
    else:
        click.echo(
            f"embedder={'enabled' if enabled else 'disabled'} dim={dim} "
            f"indexed={status['indexed_documents']}"
        )


@embed.command("status")
@_json_option
@click.pass_context
@_handle_errors
def embed_status(ctx: click.Context, as_json: bool) -> None:
    """Show the embedding queue state (pending / in_progress / done / failed)."""
    store = _get_store(ctx)
    payload = store.embedding_status()
    if as_json:
        _emit_json(payload)
        return
    click.echo(
        f"embedder={'enabled' if payload['embedder_enabled'] else 'disabled'} "
        f"dim={payload['dim']} indexed={payload['indexed_documents']}"
    )
    q = payload["queue"]
    click.echo(
        f"queue: pending={q['pending']} in_progress={q['in_progress']} "
        f"done={q['done']} failed={q['failed']}"
    )
    oldest = payload["oldest_failed"]
    if oldest:
        extra = ""
        if oldest.get("last_error"):
            extra = f" error={oldest['last_error']}"
        click.echo(f"oldest_failed: {oldest['doc_id']} attempts={oldest['attempts']}{extra}")


@embed.command("retry")
@click.argument("doc_id", required=False)
@click.option("--all", "reset_all", is_flag=True, help="Reset every failed job to pending.")
@_json_option
@click.pass_context
@_handle_errors
def embed_retry(ctx: click.Context, doc_id: str | None, reset_all: bool, as_json: bool) -> None:
    """Re-queue embedding jobs that failed.

    ``DOC_ID`` resets a single document; ``--all`` resets every failed
    job. A ``done`` or ``in_progress`` job can also be re-run by naming
    its doc id.
    """
    store = _get_store(ctx)
    if doc_id is None and not reset_all:
        click.echo("usage: kb embed retry <doc-id> | kb embed retry --all", err=True)
        sys.exit(EXIT_USAGE)
    moved = store.retry_embedding(doc_id=doc_id if doc_id is not None else None)
    if as_json:
        _emit_json({"ok": True, "retried": moved, "doc_id": doc_id})
    else:
        scope = f" for {doc_id}" if doc_id else " (all failed)"
        click.echo(f"retried {moved} job(s){scope}")
        # Retry only flips the row back to ``pending``; the store's
        # background worker drains it asynchronously. This CLI is a
        # one-shot process, so the worker may not get a chance to drain
        # before exit — run ``kb embed status`` to confirm, or keep the
        # MCP server / `kb admin start` process running to drain it.
        click.echo(
            "(queue state: run `kb embed status` to confirm; retry only "
            "re-queues the row — the background worker drains it while "
            "the store is running.)"
        )


@cli.command()
@_json_option
@click.pass_context
@_handle_errors
def doctor(ctx: click.Context, as_json: bool) -> None:
    """Run health checks on the knowledge base."""
    store = _get_store(ctx)
    report = store.doctor()
    if as_json:
        _emit_json(report.model_dump(mode="json"))
    else:
        click.echo(report.summary())


# ---- kb stats -----------------------------------------------------------------


@cli.command()
@_json_option
@click.pass_context
@_handle_errors
def stats(ctx: click.Context, as_json: bool) -> None:
    """Show knowledge base statistics."""
    store = _get_store(ctx)
    stats_data = store.stats()
    if as_json:
        _emit_json(stats_data)
    else:
        click.echo(f"Total documents: {stats_data['total_docs']}")
        click.echo(f"Total links: {stats_data['total_links']}")
        click.echo(f"Soft deleted: {stats_data['soft_deleted']}")
        click.echo(f"Changes in last 7 days: {stats_data['recent_changes']}")
        click.echo()
        click.echo("Documents by type:")
        for typ, cnt in stats_data["docs_by_type"].items():
            click.echo(f"  {typ}: {cnt}")


# ---- kb reindex ---------------------------------------------------------------


@cli.command()
@click.pass_context
@_handle_errors
def reindex(ctx: click.Context) -> None:
    """Rebuild the full-text search index."""
    store = _get_store(ctx)
    store.reindex()
    click.echo("Reindexed search index.")


# ---- kb prune ----------------------------------------------------------------


@cli.command()
@click.option(
    "--older-than",
    default=30,
    type=int,
    show_default=True,
    help="Prune documents deleted more than N days ago.",
)
@_json_option
@click.pass_context
@_handle_errors
def prune(ctx: click.Context, older_than: int, as_json: bool) -> None:
    """Permanently delete soft-deleted documents older than the specified age."""
    from datetime import timedelta

    store = _get_store(ctx)
    deleted = store.prune(timedelta(days=older_than))
    if as_json:
        _emit_json({"deleted": deleted})
    else:
        click.echo(f"Permanently deleted {deleted} documents.")


# ---- kb serve ----------------------------------------------------------------


@cli.command()
@click.option("--log-level", default="INFO", show_default=True, help="Log level.")
@click.option("--vault", help="Vault name to serve.")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    show_default=True,
    help="Transport to serve on. 'http' is streamable HTTP on /mcp.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    envvar="KB_MCP_HTTP_HOST",
    show_envvar=True,
    help="Interface to bind (http transport only).",
)
@click.option(
    "--port",
    default=8000,
    show_default=True,
    type=int,
    envvar="KB_MCP_HTTP_PORT",
    show_envvar=True,
    help="Port to bind (http transport only).",
)
@click.pass_context
@_handle_errors
def serve(
    ctx: click.Context,
    log_level: str,
    vault: str | None,
    transport: str,
    host: str,
    port: int,
) -> None:
    """Start the MCP server on stdio, or on streamable HTTP with --transport http.

    The http transport requires the KB_MCP_HTTP_TOKEN environment variable; it
    refuses to start when the variable is missing or empty. The bind address and
    port also come from KB_MCP_HTTP_HOST / KB_MCP_HTTP_PORT; an explicit
    --host / --port wins over the environment.
    """
    from kb_mcp_lite.mcp_server import run as run_mcp_server

    if log_level:
        os.environ["KB_MCP_LOG_LEVEL"] = log_level
    if vault:
        os.environ["KB_MCP_VAULT"] = vault
    run_mcp_server(vault=vault, transport=transport, host=host, port=port)


@cli.command(name="mcp", help="Alias for 'serve'. Start the MCP server.")
@click.option("--log-level", default="INFO", show_default=True, help="Log level.")
@click.option("--vault", help="Vault name to serve.")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    show_default=True,
    help="Transport to serve on. 'http' is streamable HTTP on /mcp.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    envvar="KB_MCP_HTTP_HOST",
    show_envvar=True,
    help="Interface to bind (http transport only).",
)
@click.option(
    "--port",
    default=8000,
    show_default=True,
    type=int,
    envvar="KB_MCP_HTTP_PORT",
    show_envvar=True,
    help="Port to bind (http transport only).",
)
@click.pass_context
@_handle_errors
def mcp_alias(
    ctx: click.Context,
    log_level: str,
    vault: str | None,
    transport: str,
    host: str,
    port: int,
) -> None:
    """Start the MCP server on stdio, or on streamable HTTP with --transport http."""
    ctx.forward(serve)


# ---- vault commands ----------------------------------------------------------


@cli.group(name="vault")
def vault_group() -> None:
    """Manage multiple isolated knowledge bases (vaults)."""
    pass


@vault_group.command(name="list")
@_json_option
@click.pass_context
@_handle_errors
def vault_list(ctx: click.Context, as_json: bool) -> None:
    """List all available vaults."""
    vm = ctx.obj["vault_manager"]
    vaults = vm.list_vaults()
    if as_json:
        _emit_json(
            [
                {
                    "name": v.name,
                    "path": v.path,
                    "description": v.description,
                    "sync_dir": v.sync_dir,
                }
                for v in vaults
            ]
        )
    else:
        default_vault = ctx.obj["vault_manager"].get_current()
        click.echo(f"Default vault: {default_vault}")
        click.echo()
        click.echo("Available vaults:")
        for vault in vaults:
            is_default = "*" if vault.name == default_vault else " "
            click.echo(f"{is_default} {vault.name}: {vault.path}")


@vault_group.command(name="create")
@click.argument("name")
@click.option("--desc", help="Optional description for the vault.")
@_json_option
@click.pass_context
@_handle_errors
def vault_create(ctx: click.Context, name: str, desc: str | None, as_json: bool) -> None:
    """Create a new vault."""
    vm = ctx.obj["vault_manager"]
    vault_path = vm.create(name, description=desc)
    if as_json:
        _emit_json({"name": name, "path": str(vault_path.path)})
    else:
        click.echo(f"Created vault {name} at {vault_path.path}")


@vault_group.command(name="switch")
@click.argument("name")
@click.pass_context
@_handle_errors
def vault_switch(ctx: click.Context, name: str) -> None:
    """Set the default vault."""
    vm = ctx.obj["vault_manager"]
    vm.switch(name)
    click.echo(f"Default vault set to {name}")


@vault_group.command(name="init-git")
@click.option(
    "--sync-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    required=True,
    help="Path to the Git repository to sync with.",
)
@click.pass_context
@_handle_errors
def vault_init_git(ctx: click.Context, sync_dir: str) -> None:
    """Initialize Git sync for the current vault."""
    vm = ctx.obj["vault_manager"]
    name = vm.get_current()
    output = vm.init_git(name=name, sync_dir=sync_dir)
    click.echo(output or f"Vault sync initialized with directory {sync_dir}")


@vault_group.command(name="commit")
@click.option("--message", "-m", required=True, help="Commit message.")
@click.option(
    "--full",
    "-f",
    is_flag=True,
    default=False,
    help="Force full export of all documents instead of incremental.",
)
@click.pass_context
@_handle_errors
def vault_commit(ctx: click.Context, message: str, full: bool) -> None:
    """Export changes and commit to Git."""
    vm = ctx.obj["vault_manager"]
    name = vm.get_current()
    click.echo(f"Exporting vault '{name}' and committing to Git...")
    output = vm.commit(message, name=name, full=full)
    click.echo(output or "Changes committed to Git.")


@vault_group.command(name="push")
@click.argument("remote", default="origin")
@click.argument("branch", default="main")
@click.pass_context
@_handle_errors
def vault_push(ctx: click.Context, remote: str, branch: str) -> None:
    """Push committed changes to remote Git repository."""
    vm = ctx.obj["vault_manager"]
    name = vm.get_current()
    click.echo(f"Pushing committed changes to remote '{remote}' (branch: '{branch}')...")
    output = vm.push(remote=remote, branch=branch, name=name)
    click.echo(output or "Changes pushed to remote.")


@vault_group.command(name="pull")
@click.argument("remote", default="origin")
@click.argument("branch", default="main")
@click.pass_context
@_handle_errors
def vault_pull(ctx: click.Context, remote: str, branch: str) -> None:
    """Pull latest changes from remote Git repository and import them."""
    vm = ctx.obj["vault_manager"]
    name = vm.get_current()
    click.echo(f"Pulling latest changes from remote '{remote}' (branch: '{branch}')...")
    output = vm.pull(remote=remote, branch=branch, name=name)
    click.echo(output or "Changes pulled and imported.")


@vault_group.command(name="status")
@click.pass_context
@_handle_errors
def vault_status(ctx: click.Context) -> None:
    """Show the Git status of the current vault."""
    vm = ctx.obj["vault_manager"]
    name = vm.get_current()
    output = vm.status(name=name)
    click.echo(output)


@vault_group.command(name="sync")
@click.option("--message", "-m", default="sync: auto-commit local changes", help="Commit message.")
@click.option("--remote", default="origin", help="Git remote name.")
@click.option("--branch", default="main", help="Git branch name.")
@click.pass_context
@_handle_errors
def vault_sync(ctx: click.Context, message: str, remote: str, branch: str) -> None:
    """Export, commit, pull (merge/import), and push changes to remote."""
    vm = ctx.obj["vault_manager"]
    name = vm.get_current()
    click.echo(f"Starting bi-directional sync for vault '{name}'...")
    output = vm.sync(message=message, remote=remote, branch=branch, name=name)
    click.echo(output)


# ---- admin commands ----------------------------------------------------------


@cli.group(name="admin")
def admin_group() -> None:
    """Web administration interface commands."""
    pass


@admin_group.command(name="start")
@click.option("--port", default=8888, type=int, help="Port to run the admin server on.")
@click.pass_context
@_handle_errors
def admin_start(ctx: click.Context, port: int) -> None:
    """Start the web administration interface."""
    from kb_mcp_lite.admin import run_admin

    store = _get_store(ctx)
    run_admin(store=store, port=port)


# ---- watch command -----------------------------------------------------------


@cli.command(name="watch")
@click.option(
    "--interval", default=1.0, type=float, help="Poll interval in seconds (poll mode only)."
)
@click.option(
    "--mode",
    type=click.Choice(["auto", "event", "poll"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Watcher mode: 'auto' picks event when available, 'event' uses watchfiles, 'poll' uses os.walk loop.",
)
@click.option(
    "--debounce-ms",
    default=200,
    type=int,
    show_default=True,
    help="Debounce window in milliseconds (event mode only).",
)
@click.pass_context
@_handle_errors
def watch_command(ctx: click.Context, interval: float, mode: str, debounce_ms: int) -> None:
    """Watch the Markdown directory and auto-sync changes to SQLite."""
    from kb_mcp_lite.watcher import VaultWatcher, WatchMode, resolve_mode

    vm = VaultManager()
    vault_name = vm.get_current()
    watcher = VaultWatcher(vault_name=vault_name, vault_manager=vm)
    resolved = resolve_mode(cast(WatchMode, mode.lower()))
    click.echo(f"Watching vault '{vault_name}' markdown directory at: {watcher.watch_dir}")
    click.echo(f"Mode: {mode} -> {resolved}")
    click.echo("Press Ctrl+C to stop.")
    try:
        watcher.run(
            interval_seconds=interval, mode=cast(WatchMode, mode.lower()), debounce_ms=debounce_ms
        )
    except KeyboardInterrupt:
        click.echo("\nStopped watching.")


# ---- diff-check command ------------------------------------------------------


@cli.command(name="diff-check")
@_json_option
@click.pass_context
@_handle_errors
def diff_check_command(ctx: click.Context, as_json: bool) -> None:
    """Analyze current git diff and recommend related ADRs, lessons, and constraints."""
    from kb_mcp_lite.context_guard import ContextGuard

    store = _get_store(ctx)
    guard = ContextGuard(store=store)
    result = guard.evaluate_diff()

    if as_json:
        _emit_json(result)
    else:
        if not result["has_recommendations"]:
            click.echo("No related architectural decisions or lessons found for current changes.")
            return

        click.echo(result["prompt_context"])


# ---- kb scheduler (v0.8 特性 #7) -------------------------------------------


@cli.group(name="scheduler", invoke_without_command=True)
@click.pass_context
def scheduler_group(ctx: click.Context) -> None:
    """Manage scheduled tasks (auto-commit, auto-embed, etc.)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@scheduler_group.command("list")
@_json_option
@click.pass_context
@_handle_errors
def scheduler_list(ctx: click.Context, as_json: bool) -> None:
    """List all registered scheduled tasks."""
    from kb_mcp_lite.scheduler import TASK_REGISTRY

    tasks = [{"name": name, "description": cls.description} for name, cls in TASK_REGISTRY.items()]
    if as_json:
        _emit_json(tasks)
    else:
        if not tasks:
            click.echo("No scheduled tasks registered.")
            return
        for t in tasks:
            click.echo(f"  {t['name']:20s} {t['description']}")


@scheduler_group.command("status")
@_json_option
@click.pass_context
@_handle_errors
def scheduler_status(ctx: click.Context, as_json: bool) -> None:
    """Show scheduler status and next run times."""
    from kb_mcp_lite.scheduler import TaskScheduler

    store = _get_store(ctx)
    from kb_mcp_lite.config import load_config

    config = load_config()
    scheduler = TaskScheduler(store, config)
    status = scheduler.get_status()
    if as_json:
        _emit_json(status)
    else:
        click.echo(f"Running: {status['running']}")
        click.echo(f"Jobs: {len(status['jobs'])}")
        for job in status["jobs"]:
            next_run = job.get("next_run", "N/A")
            click.echo(f"  {job['id']}: next={next_run}")


@scheduler_group.command("run")
@click.argument("task_name")
@_json_option
@click.pass_context
@_handle_errors
def scheduler_run(ctx: click.Context, task_name: str, as_json: bool) -> None:
    """Manually trigger a scheduled task."""
    from kb_mcp_lite.scheduler import TaskScheduler

    store = _get_store(ctx)
    from kb_mcp_lite.config import load_config

    config = load_config()
    scheduler = TaskScheduler(store, config)
    run = scheduler.run_task_now(task_name)
    if as_json:
        _emit_json(
            {
                "task_name": run.task_name,
                "status": run.status,
                "duration_ms": run.duration_ms,
                "error": run.error,
            }
        )
    else:
        if run.status == "ok":
            click.echo(f"Task {run.task_name} completed in {run.duration_ms}ms")
        else:
            click.echo(f"Task {run.task_name} FAILED: {run.error}")


@scheduler_group.command("history")
@click.option("--limit", default=20, type=int, help="Number of records to show.")
@_json_option
@click.pass_context
@_handle_errors
def scheduler_history(ctx: click.Context, limit: int, as_json: bool) -> None:
    """Show task execution history."""
    store = _get_store(ctx)
    rows = store._conn.execute(
        """
        SELECT task_name, started_at, finished_at, status, duration_ms, error, triggered_by
        FROM schedule_history
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if as_json:
        _emit_json([dict(r) for r in rows])
    else:
        if not rows:
            click.echo("No execution history.")
            return
        for r in rows:
            status_mark = "✓" if r["status"] == "ok" else "✗"
            err = f" [{r['error']}]" if r["error"] else ""
            click.echo(
                f"  {status_mark} {r['task_name']:20s} {r['started_at'][:19]} "
                f"{r['duration_ms']}ms ({r['triggered_by']}){err}"
            )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()


__all__ = [
    "cli",
    "main",
    "EXIT_OK",
    "EXIT_VALIDATION",
    "EXIT_NOT_FOUND",
    "EXIT_CONFLICT",
    "EXIT_INTERNAL",
    "EXIT_USAGE",
]
