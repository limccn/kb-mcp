"""Typed relation vocabulary and impact analysis (v0.8 特性 #6).

Provides a standard set of semantic relation types with traversal metadata,
an impact analyzer that follows influence edges, and a supersession chain tracer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from kb_mcp_lite.store.sqlite import SqliteStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Relation specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelationSpec:
    """Metadata for a named relation type."""

    name: str
    forward_label: str  # "A supersedes B" from A's perspective
    backward_label: str  # "A supersedes B" from B's perspective
    default_direction: Literal["forward", "backward", "bidirectional"]
    traversal_cost: float  # BFS weight — higher cost = less preferred traversal
    is_influence: bool  # included in impact analysis
    is_supersession: bool  # part of the decision evolution chain
    description: str


STANDARD_RELATIONS: dict[str, RelationSpec] = {
    "relates-to": RelationSpec(
        name="relates-to",
        forward_label="与...相关",
        backward_label="与...相关",
        default_direction="bidirectional",
        traversal_cost=1.0,
        is_influence=False,
        is_supersession=False,
        description="通用关联，无方向语义",
    ),
    "supersedes": RelationSpec(
        name="supersedes",
        forward_label="取代了",
        backward_label="被...取代",
        default_direction="forward",
        traversal_cost=0.8,
        is_influence=True,
        is_supersession=True,
        description="本决策/方案取代了目标（用于决策追溯）",
    ),
    "superseded-by": RelationSpec(
        name="superseded-by",
        forward_label="被...取代",
        backward_label="取代了",
        default_direction="forward",
        traversal_cost=0.8,
        is_influence=True,
        is_supersession=True,
        description="本决策/方案已被目标取代（supersedes 的反向）",
    ),
    "depends-on": RelationSpec(
        name="depends-on",
        forward_label="依赖于",
        backward_label="被...依赖",
        default_direction="forward",
        traversal_cost=0.9,
        is_influence=True,
        is_supersession=False,
        description="本方案/组件的运行依赖目标",
    ),
    "blocks": RelationSpec(
        name="blocks",
        forward_label="阻塞",
        backward_label="被...阻塞",
        default_direction="forward",
        traversal_cost=0.9,
        is_influence=True,
        is_supersession=False,
        description="本任务阻塞了目标任务的执行",
    ),
    "implements": RelationSpec(
        name="implements",
        forward_label="实现了",
        backward_label="被...实现",
        default_direction="forward",
        traversal_cost=0.7,
        is_influence=False,
        is_supersession=False,
        description="本组件/模块实现了目标的接口/规范",
    ),
    "references": RelationSpec(
        name="references",
        forward_label="引用了",
        backward_label="被...引用",
        default_direction="forward",
        traversal_cost=0.6,
        is_influence=False,
        is_supersession=False,
        description="正文中提到的目标（自动提取）",
    ),
    "governs": RelationSpec(
        name="governs",
        forward_label="约束了",
        backward_label="受...约束",
        default_direction="forward",
        traversal_cost=0.8,
        is_influence=True,
        is_supersession=False,
        description="本规范/政策约束了目标的实现方式",
    ),
    "owned-by": RelationSpec(
        name="owned-by",
        forward_label="归属于",
        backward_label="拥有...的所有权",
        default_direction="forward",
        traversal_cost=0.5,
        is_influence=False,
        is_supersession=False,
        description="本资源/项目的负责人",
    ),
    "tagged-with": RelationSpec(
        name="tagged-with",
        forward_label="标记为",
        backward_label="包含...标签",
        default_direction="forward",
        traversal_cost=0.4,
        is_influence=False,
        is_supersession=False,
        description="通过 tag 关联的同主题文档",
    ),
    "contains": RelationSpec(
        name="contains",
        forward_label="包含了",
        backward_label="隶属于",
        default_direction="forward",
        traversal_cost=0.5,
        is_influence=False,
        is_supersession=False,
        description="目录树关系，kb ingest 生成",
    ),
}


def get_relation_spec(name: str) -> RelationSpec | None:
    """Return the RelationSpec for *name*, or None if not a standard relation."""
    return STANDARD_RELATIONS.get(name)


def resolve_relation(name: str) -> RelationSpec:
    """Return the RelationSpec for *name*, falling back to relates-to."""
    return STANDARD_RELATIONS.get(name, STANDARD_RELATIONS["relates-to"])


# ---------------------------------------------------------------------------
# Impact analyzer
# ---------------------------------------------------------------------------

from kb_mcp_lite.schema import Document  # noqa: E402 (avoid circular)


@dataclass
class ImpactNode:
    """One node in an impact analysis result."""

    doc: Document
    distance: int  # hops from root
    via: str  # doc_id of the node that reached this one
    rel: str  # relation type used to reach this node
    path: list[str]  # full path from root, e.g. ["root", "a", "b"]


class ImpactAnalyzer:
    """Traverse influence edges outward from a root document.

    Only follows edges whose relation ``is_influence=True`` in the vocabulary.
    """

    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    def analyze(
        self,
        root_id: str,
        max_depth: int = 3,
        max_results: int = 50,
    ) -> list[ImpactNode]:
        """Return all documents reachable via influence edges from *root_id*.

        Results are ordered by distance (closest first), then by doc title.
        """
        # Collect influence relation names
        influence_rels = {name for name, spec in STANDARD_RELATIONS.items() if spec.is_influence}
        if not influence_rels:
            return []

        visited: dict[str, tuple[int, str, str, list[str]]] = {}  # id → (dist, via, rel, path)
        current_frontier = [root_id]
        root_path = [root_id]

        for depth in range(1, max_depth + 1):
            if not current_frontier:
                break
            next_frontier: list[str] = []
            placeholders = ",".join("?" for _ in current_frontier)

            # Outbound: from_id IN frontier AND rel IN influence_rels
            rel_list = list(influence_rels)
            rel_placeholders = ",".join("?" for _ in rel_list)
            rows = self.store._conn.execute(
                f"""
                SELECT from_id, to_id, rel
                FROM links
                WHERE from_id IN ({placeholders})
                  AND rel IN ({rel_placeholders})
                """,
                current_frontier + rel_list,
            ).fetchall()

            # Inbound: to_id IN frontier AND rel IN influence_rels
            rows_in = self.store._conn.execute(
                f"""
                SELECT to_id AS from_id, from_id AS to_id, rel
                FROM links
                WHERE to_id IN ({placeholders})
                  AND rel IN ({rel_placeholders})
                """,
                current_frontier + rel_list,
            ).fetchall()
            rows = rows + rows_in

            for r in rows:
                src, dst, rel_name = r["from_id"], r["to_id"], r["rel"]
                if dst in visited or dst == root_id:
                    continue
                parent_path = visited.get(src, (0, "", "", root_path))[3]
                new_path = parent_path + [dst]
                visited[dst] = (depth, src, rel_name, new_path)
                next_frontier.append(dst)

            current_frontier = next_frontier

        # Build results
        results: list[ImpactNode] = []
        for doc_id, (dist, via, rel_name, path) in sorted(visited.items(), key=lambda x: x[1][0]):
            try:
                doc = self.store.get(doc_id)
                results.append(ImpactNode(doc=doc, distance=dist, via=via, rel=rel_name, path=path))
            except Exception:
                logger.warning("Impact analyzer: cannot load doc %s", doc_id)
            if len(results) >= max_results:
                break
        return results


def supersession_chain(store: SqliteStore, decision_id: str) -> list[str]:
    """Follow supersession edges to build the decision evolution chain.

    Returns a list of doc IDs starting from *decision_id*, following
    ``supersedes`` / ``superseded-by`` edges in both directions.
    E.g. ``[v1-dec, v2-dec, v3-dec]``.

    Only edges whose relation ``is_supersession=True`` are followed.
    """
    supersession_rels = {name for name, spec in STANDARD_RELATIONS.items() if spec.is_supersession}
    if not supersession_rels:
        return [decision_id]

    chain = [decision_id]
    visited = {decision_id}

    # Walk forward (what does this decision supersede? what supersedes it?)
    current = decision_id
    while True:
        rel_list = list(supersession_rels)
        placeholders_rel = ",".join("?" for _ in rel_list)
        rows = store._conn.execute(
            f"""
            SELECT to_id AS neighbor, rel FROM links
            WHERE from_id = ? AND rel IN ({placeholders_rel})
            UNION
            SELECT from_id AS neighbor, rel FROM links
            WHERE to_id = ? AND rel IN ({placeholders_rel})
            """,
            [current] + rel_list + [current] + rel_list,
        ).fetchall()

        next_doc = None
        for r in rows:
            neighbor = r["neighbor"]
            if neighbor not in visited:
                next_doc = neighbor
                break

        if next_doc is None:
            break
        visited.add(next_doc)
        chain.append(next_doc)
        current = next_doc

    return chain
