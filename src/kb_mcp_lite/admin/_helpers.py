"""Shared helpers for admin route modules."""

from __future__ import annotations

import os
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from kb_mcp_lite.schema import Document, Link, SearchHit, ValidationError
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.vault import VaultManager

BUILTIN_TYPES: list[dict[str, Any]] = [
    {
        "name": "project",
        "label": "项目/规划",
        "description": "项目、代码库、架构栈、技术选型与负责人",
        "color": "#3b82f6",
        "is_builtin": True,
    },
    {
        "name": "decision",
        "label": "架构决策",
        "description": "ADR 决策记录（背景 → 选型决策 → 带来的后果）",
        "color": "#8b5cf6",
        "is_builtin": True,
    },
    {
        "name": "lesson",
        "label": "经验复盘",
        "description": "故障复盘、踩坑经验与避坑防范规则",
        "color": "#10b981",
        "is_builtin": True,
    },
    {
        "name": "glossary",
        "label": "术语定义",
        "description": "业务专有名词与核心概念解释",
        "color": "#f59e0b",
        "is_builtin": True,
    },
    {
        "name": "person",
        "label": "团队角色",
        "description": "团队成员、业务与技术负责人信息",
        "color": "#ec4899",
        "is_builtin": True,
    },
    {
        "name": "faq",
        "label": "常见问答",
        "description": "常见问题汇总（标题为问题，正文为解答）",
        "color": "#06b6d4",
        "is_builtin": True,
    },
    {
        "name": "api",
        "label": "接口约定",
        "description": "API 规范、请求响应协议与接口 Mock 定义",
        "color": "#6366f1",
        "is_builtin": True,
    },
    {
        "name": "runbook",
        "label": "运维手册",
        "description": "SOP 操作流程、部署上线与故障排查步骤",
        "color": "#f43f5e",
        "is_builtin": True,
    },
    {
        "name": "release",
        "label": "发布说明",
        "description": "版本发布记录与变更日志",
        "color": "#14b8a6",
        "is_builtin": True,
    },
]

DOC_TYPES = [t["name"] for t in BUILTIN_TYPES]
SEARCH_MODES = ["lexical", "fuzzy", "semantic", "hybrid"]


def get_custom_types() -> list[dict[str, Any]]:
    from kb_mcp_lite.config import load_config

    cfg = load_config()
    types_val = cfg.get("types", [])
    if isinstance(types_val, list):
        return [t for t in types_val if isinstance(t, dict) and t.get("name")]
    return []


def save_custom_types(custom_types: list[dict[str, Any]]) -> None:
    import yaml
    from kb_mcp_lite.config import config_path, load_config

    p = config_path()
    cfg = load_config()
    cfg["types"] = custom_types
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")


def get_all_types(store: SqliteStore | None = None) -> list[dict[str, Any]]:
    # Start with built-in types
    type_map: dict[str, dict[str, Any]] = {
        t["name"]: dict(t) for t in BUILTIN_TYPES
    }
    # Merge custom types from config
    for ct in get_custom_types():
        name = str(ct.get("name", "")).strip()
        if not name:
            continue
        if name in type_map:
            type_map[name].update(ct)
        else:
            type_map[name] = {
                "name": name,
                "label": ct.get("label", name),
                "description": ct.get("description", ""),
                "color": ct.get("color", "#64748b"),
                "is_builtin": False,
            }

    # Query database for document counts and extra types
    doc_counts: dict[str, int] = {}
    if store is not None:
        try:
            rows = store._conn.execute(
                "SELECT type, COUNT(*) AS cnt FROM documents WHERE deleted_at IS NULL GROUP BY type"
            ).fetchall()
            for r in rows:
                t_name = str(r["type"])
                doc_counts[t_name] = int(r["cnt"])
                if t_name not in type_map:
                    type_map[t_name] = {
                        "name": t_name,
                        "label": t_name,
                        "description": "数据库中存在的历史文档类型",
                        "color": "#64748b",
                        "is_builtin": False,
                    }
        except Exception:
            pass

    result = []
    builtin_names = {t["name"]: idx for idx, t in enumerate(BUILTIN_TYPES)}
    for name, item in type_map.items():
        item_copy = dict(item)
        item_copy["doc_count"] = doc_counts.get(name, 0)
        result.append(item_copy)

    result.sort(
        key=lambda x: (
            0 if x.get("is_builtin") else 1,
            builtin_names.get(x["name"], 999),
            x["name"],
        )
    )
    return result


def get_doc_type_names(store: SqliteStore | None = None) -> list[str]:
    all_types = get_all_types(store)
    return [t["name"] for t in all_types]



def create_default_store() -> SqliteStore:
    try:
        mgr = VaultManager()
        db_path = mgr.resolve_path()
        return SqliteStore(db_path)
    except Exception:
        home = os.environ.get("KB_MCP_HOME")
        if home:
            db_path = Path(home) / "kb.db"
        else:
            db_path = Path.home() / ".local" / "share" / "kb-mcp" / "kb.db"
        return SqliteStore(db_path)


@contextmanager
def open_store(app: FastAPI):
    store = SqliteStore(Path(app.state.store_path))
    try:
        yield store
    finally:
        store.close()


def split_tags(raw: str) -> list[str] | None:
    values = [tag.strip() for tag in raw.split(",") if tag.strip()]
    return values or None


def filtered_documents(
    store: SqliteStore,
    *,
    q: str = "",
    doc_type: str = "",
    tag: str = "",
    include_deleted: bool = False,
) -> list[Document]:
    tags = [tag] if tag else None
    if q.strip():
        hits = store.search(q, type=doc_type or None, tags=tags, limit=100, mode="hybrid")
        return [hit.doc for hit in hits]
    return store.list(
        type=doc_type or None,
        tags=tags,
        limit=200,
        include_deleted=include_deleted,
    )


def create_document(
    store: SqliteStore,
    *,
    doc_id: str,
    doc_type: str,
    title: str,
    tags: list[str] | None,
    source: str | None,
    body: str,
) -> Document:
    doc = Document(
        id=(doc_id or "").strip(),
        type=doc_type.strip(),
        title=title.strip(),
        tags=tags or [],
        source=source.strip() if isinstance(source, str) and source.strip() else None,
        body=body,
    )
    created_id = store.add(doc)
    return store.get(created_id)


def patch_document(
    store: SqliteStore,
    doc_id: str,
    title: str | None,
    tags: list[str] | None,
    source: str | None,
    body: str | None,
    deleted: bool | None,
) -> Document:
    if deleted is True:
        store.delete(doc_id)
        return store.get(doc_id, include_deleted=True)
    fields: dict[str, object] = {}
    if title is not None:
        fields["title"] = title.strip()
    if tags is not None:
        fields["tags"] = tags
    if source is not None:
        fields["source"] = source.strip() or None
    if body is not None:
        fields["body"] = body
    if not fields:
        raise ValidationError("update requires at least one field")
    return store.update(doc_id, **fields)


def doc_row(store: SqliteStore, doc: Document) -> dict[str, Any]:
    return {
        "doc": doc,
        "outlinks": len(store.outlinks(doc.id)),
        "backlinks": len(store.backlinks(doc.id)),
    }


def doc_form_data(doc: Document) -> dict[str, Any]:
    return {
        "id": doc.id,
        "type": doc.type,
        "title": doc.title,
        "tags": ", ".join(doc.tags),
        "source": doc.source or "",
        "body": doc.body,
    }


def count_links(store: SqliteStore) -> int:
    return int(store._conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])


def list_links(store: SqliteStore) -> list[Link]:
    rows = store._conn.execute(
        "SELECT from_id, to_id, rel, created_at FROM links ORDER BY created_at DESC, from_id, to_id"
    ).fetchall()
    return [store._row_to_link(row) for row in rows]


def serialize_doc(doc: Document) -> dict[str, Any]:
    payload = doc.model_dump(mode="json")
    payload["tags"] = list(doc.tags)
    return payload


def serialize_link(link: Link) -> dict[str, Any]:
    return link.model_dump(mode="json")


def serialize_hit(hit: SearchHit) -> dict[str, Any]:
    return {
        "doc": serialize_doc(hit.doc),
        "snippet": hit.snippet,
        "score": hit.score,
    }


def json_error(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def flash_url(base: str, kind: str, message: str) -> str:
    return f"{base}?{urlencode({'flash': kind, 'message': message})}"


def overview_payload(store: SqliteStore) -> dict[str, Any]:
    all_docs = store.export_all(include_deleted=True)
    active_docs = [doc for doc in all_docs if doc.deleted_at is None]
    deleted_docs = [doc for doc in all_docs if doc.deleted_at is not None]
    tag_counts = Counter(tag for doc in active_docs for tag in doc.tags)
    type_counts = Counter(doc.type for doc in active_docs)
    doctor_report = store.doctor()
    recent_docs = sorted(active_docs, key=lambda doc: doc.updated_at, reverse=True)[:8]
    orphan_count = sum(
        1 for doc in active_docs if not store.backlinks(doc.id) and not store.outlinks(doc.id)
    )
    conflict_count = sum(1 for doc in active_docs if doc.type == "conflict")
    try:
        embedder = getattr(store, "_embedder", None)
        embed_enabled = bool(embedder and getattr(embedder, "enabled", False))
        embed_dim = getattr(embedder, "dim", 0) if embed_enabled else 0
        vec_count = (
            store._conn.execute("SELECT COUNT(*) FROM docs_vec").fetchone()[0]
            if embed_enabled
            else 0
        )
    except Exception:
        embed_enabled = False
        embed_dim = 0
        vec_count = 0
    return {
        "stats": {
            "documents": len(active_docs),
            "deleted_documents": len(deleted_docs),
            "types": len(type_counts),
            "links": count_links(store),
            "orphan_documents": orphan_count,
            "vectors": vec_count,
            "conflicts": conflict_count,
        },
        "type_counts": sorted(type_counts.items()),
        "tag_counts": tag_counts.most_common(12),
        "recent_docs": recent_docs,
        "doctor_report": doctor_report,
        "embed_enabled": embed_enabled,
        "embed_dim": embed_dim,
    }


def schema_version(store: SqliteStore) -> str:
    row = store._conn.execute(
        "SELECT version, name FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "unknown"
    return f"{row['version']} ({row['name']})"


__all__ = [
    "BUILTIN_TYPES",
    "DOC_TYPES",
    "SEARCH_MODES",
    "create_default_store",
    "open_store",
    "split_tags",
    "filtered_documents",
    "create_document",
    "patch_document",
    "doc_row",
    "doc_form_data",
    "count_links",
    "list_links",
    "serialize_doc",
    "serialize_link",
    "serialize_hit",
    "json_error",
    "flash_url",
    "overview_payload",
    "schema_version",
    "get_custom_types",
    "save_custom_types",
    "get_all_types",
    "get_doc_type_names",
]

