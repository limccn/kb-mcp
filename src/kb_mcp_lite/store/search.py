"""Search mixin for SqliteStore — FTS5 + vec0 search operations."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any, Literal

from kb_mcp_lite.schema import Document, RelatedDoc, SearchHit, ValidationError

if TYPE_CHECKING:
    import sqlite3

    from kb_mcp_lite.reranker import Reranker

    _conn: sqlite3.Connection
    _vec_row_is_tuple: bool

    def _vec_conn_lazy(self) -> Any: ...
    def get(self, doc_id: str, include_deleted: bool = False) -> Document: ...


logger = logging.getLogger("kb_mcp_lite.store.search")


class SearchMixin:
    """Mixin providing search methods.

    Requires the host class to expose ``self._conn``,
    ``self._vec_conn_lazy()``, and ``self.get()``.
    """

    if TYPE_CHECKING:
        _conn: sqlite3.Connection
        _vec_row_is_tuple: bool

        def _vec_conn_lazy(self) -> Any: ...
        def get(self, doc_id: str, include_deleted: bool = False) -> Document: ...

    def search(
        self,
        query: str,
        type: str | None = None,  # noqa: A002
        tags: list[str] | None = None,
        limit: int = 10,
        mode: str = "lexical",
        rrf_k: int = 60,
        expand_graph: bool = True,
        max_neighbors: int = 5,
        decay: float = 0.6,
        rerank: bool = False,
        reranker: Reranker | None = None,
    ) -> list[SearchHit]:
        """Full-text search via the backend's FTS engine.

        ``mode`` selects the scoring strategy:

        - ``"lexical"`` (default): exact-token BM25 on ``docs_fts``.
        - ``"fuzzy"``: trigram BM25 on ``docs_fts_trgm``.
        - ``"semantic"``: vector similarity via ``docs_vec``.
        - ``"hybrid"`` / ``"rrf"``: reciprocal-rank fusion of all three.

        ``limit`` is capped at 100. ``rrf_k`` sets the RRF constant (default 60).

        When ``rerank=True``, retrieves a wider candidate set and applies Cross-Encoder
        re-scoring using the configured reranking model.

        When ``expand_graph=True`` (default), each top hit is enriched with
        its 1-hop graph neighbors as ``related`` documents.
        """
        query = (query or "").strip()
        if not query:
            raise ValidationError("query must not be empty")
        if limit < 1 or limit > 100:
            raise ValidationError("limit must be in 1..100")
        if mode not in ("lexical", "fuzzy", "semantic", "hybrid", "rrf"):
            raise ValidationError(
                f"mode must be one of 'lexical', 'fuzzy', 'semantic', "
                f"'hybrid', or 'rrf' (got {mode!r})"
            )

        fetch_limit = min(100, max(limit * 3, 20)) if rerank else limit

        if mode == "lexical":
            hits = self._search_fts(
                query, type=type, tags=tags, limit=fetch_limit, table="docs_fts"
            )
        elif mode == "fuzzy":
            hits = self._search_fts(
                query, type=type, tags=tags, limit=fetch_limit, table="docs_fts_trgm"
            )
        elif mode == "semantic":
            hits = self._search_semantic(query, type=type, tags=tags, limit=fetch_limit)
        else:
            hits = self._search_rrf(query, type=type, tags=tags, limit=fetch_limit, k=rrf_k)

        if rerank and hits:
            from kb_mcp_lite.reranker import make_reranker

            active_reranker = reranker or make_reranker()
            if active_reranker.enabled:
                doc_texts = [f"{h.doc.title}\n{h.doc.body[:2000]}" for h in hits]
                try:
                    ranked_items = active_reranker.rerank(query, doc_texts, top_n=limit)
                    reranked_hits: list[SearchHit] = []
                    for item in ranked_items:
                        if 0 <= item.index < len(hits):
                            h = hits[item.index]
                            reranked_hits.append(
                                SearchHit(
                                    doc=h.doc,
                                    snippet=h.snippet,
                                    score=item.score,
                                    related=h.related,
                                )
                            )
                    hits = reranked_hits
                except Exception as e:
                    logger.warning("Reranking failed: %s; falling back to initial ranking", e)
                    hits = hits[:limit]
            else:
                hits = hits[:limit]
        else:
            hits = hits[:limit]

        if expand_graph and hits:
            hits = self._expand_with_graph(hits, max_neighbors=max_neighbors, decay=decay)

        return hits

    def _search_fts(
        self,
        query: str,
        type: str | None,
        tags: list[str] | None,
        limit: int,
        table: str,
    ) -> list[SearchHit]:
        """Run a single FTS5 query and return SearchHit list."""
        if table == "docs_fts_trgm":
            fts_q = self._escape_fts(query)
        else:
            fts_q = self._escape_fts_lexical(query)
        sql = f"""
            SELECT d.*,
                   snippet({table}, 1, '<b>', '</b>', '…', 12) AS snip,
                   bm25({table}) AS score
            FROM {table}
            JOIN documents d ON d.rowid = {table}.rowid
            WHERE {table} MATCH ?
              AND d.deleted_at IS NULL
        """
        params: list[object] = [fts_q]
        if type:
            sql += " AND d.type = ?"
            params.append(type)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)

        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            raise ValidationError(f"invalid FTS query {query!r}: {e}") from e

        hits: list[SearchHit] = []
        for r in rows:
            d = dict(r)
            snippet_text = d.pop("snip", "") or ""
            score = float(d.pop("score", 0.0))
            doc = self._row_to_doc_dict(d)
            hits.append(SearchHit(doc=doc, snippet=snippet_text, score=score))

        if tags:
            wanted: set[str] = set(tags)
            hits = [h for h in hits if wanted.issubset(set(h.doc.tags))]
        return hits

    def _search_rrf(
        self,
        query: str,
        type: str | None,
        tags: list[str] | None,
        limit: int,
        k: int = 60,
    ) -> list[SearchHit]:
        """Weighted reciprocal-rank fusion of lexical + fuzzy + semantic results."""
        fetch_n = limit * 3
        lexical = self._search_fts(query, type=type, tags=tags, limit=fetch_n, table="docs_fts")
        # If strict AND yields no results, retry with OR to extract exact term matches
        if not lexical and len(query.split()) > 1:
            or_query = " OR ".join(f'"{t.strip()}"' for t in query.split() if t.strip())
            try:
                # Bypass strict AND by injecting a custom OR query safely
                sql = """
                    SELECT d.*,
                           snippet(docs_fts, 1, '<b>', '</b>', '…', 12) AS snip,
                           bm25(docs_fts) AS score
                    FROM docs_fts
                    JOIN documents d ON d.rowid = docs_fts.rowid
                    WHERE docs_fts MATCH ?
                      AND d.deleted_at IS NULL
                """
                params: list[Any] = [or_query]
                if type:
                    sql += " AND d.type = ?"
                    params.append(type)
                sql += " ORDER BY score LIMIT ?"
                params.append(fetch_n)
                rows = self._conn.execute(sql, params).fetchall()
                for r in rows:
                    d = dict(r)
                    snippet_text = d.pop("snip", "") or ""
                    score = float(d.pop("score", 0.0))
                    doc = self._row_to_doc_dict(d)
                    lexical.append(SearchHit(doc=doc, snippet=snippet_text, score=score))
            except sqlite3.OperationalError:
                pass

        fuzzy = self._search_fts(query, type=type, tags=tags, limit=fetch_n, table="docs_fts_trgm")
        try:
            semantic = self._search_semantic(query, type=type, tags=tags, limit=fetch_n)
        except Exception:
            semantic = []

        # Define channel weights: lexical has high precision, semantic has high intent, fuzzy is backup
        w_lexical = 1.6
        w_fuzzy = 0.4
        w_semantic = 1.2

        # Adjust weights if semantic search is disabled
        if not semantic:
            w_lexical = 2.0
            w_fuzzy = 0.4
            k = 30  # Adjust penalty constant for fewer channels

        rrf_scores: dict[str, tuple[float, str, Document]] = {}

        def merge_hit(hit: SearchHit, rank: int, weight: float) -> None:
            contrib = weight * (1.0 / (k + rank))
            cur_score, cur_snip, doc = rrf_scores.get(hit.doc.id, (0.0, "", hit.doc))

            # Smart Snippet Merge: Prefer snippets containing HTML tags like <b>
            new_snip = hit.snippet or ""
            if cur_snip:
                has_html_cur = "<b>" in cur_snip
                has_html_new = "<b>" in new_snip
                # Keep the current one if it has HTML highlight and the new one doesn't
                if has_html_cur and not has_html_new:
                    snippet = cur_snip
                else:
                    snippet = new_snip
            else:
                snippet = new_snip

            rrf_scores[hit.doc.id] = (cur_score + contrib, snippet, doc)

        for rank_1based, hit in enumerate(lexical, start=1):
            merge_hit(hit, rank_1based, w_lexical)
        for rank_1based, hit in enumerate(fuzzy, start=1):
            merge_hit(hit, rank_1based, w_fuzzy)
        for rank_1based, hit in enumerate(semantic, start=1):
            merge_hit(hit, rank_1based, w_semantic)

        sorted_ids = sorted(rrf_scores.keys(), key=lambda i: -rrf_scores[i][0])
        hits: list[SearchHit] = []
        for doc_id in sorted_ids:
            score, snippet, doc = rrf_scores[doc_id]
            hits.append(SearchHit(doc=doc, snippet=snippet, score=round(score, 6)))
        if tags:
            wanted: set[str] = set(tags)
            hits = [h for h in hits if wanted.issubset(set(h.doc.tags))]
        return hits[:limit]

    def _search_semantic(
        self,
        query: str,
        type: str | None,
        tags: list[str] | None,
        limit: int,
    ) -> list[SearchHit]:
        """Vector similarity search via the vec0 ``docs_vec`` table."""
        emb = getattr(self, "_embedder", None)
        if emb is None or not getattr(emb, "enabled", False):
            raise ValidationError("semantic search requires an enabled embedder")
        vec_conn = self._vec_conn_lazy()
        if vec_conn is None:
            raise ValidationError("vec0 extension not available; semantic search disabled")
        try:
            from sqlite_vec import serialize_float32
        except ImportError as e:
            raise ValidationError("sqlite-vec not installed; run: pip install 'kb-mcp[vec]'") from e

        query_vec = emb.embed(query)
        sql = """
            SELECT d.*, v.distance AS vec_distance
            FROM docs_vec v
            JOIN documents d ON d.rowid = v.rowid
            WHERE v.embedding MATCH ?
              AND k = ?
              AND d.deleted_at IS NULL
        """
        params: list[object] = [serialize_float32(query_vec), limit * 4]
        if type:
            sql += " AND d.type = ?"
            params.append(type)
        sql += " ORDER BY v.distance"

        try:
            rows = vec_conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            raise ValidationError(f"vec0 query failed: {e}") from e

        row_is_tuple = getattr(self, "_vec_row_is_tuple", False)
        hits: list[SearchHit] = []
        for r in rows:
            if row_is_tuple:
                # ``d.id`` is always the first column of ``SELECT d.*`` and
                # the distance is always the last selected column, so both
                # indexes stay correct when ``documents`` gains columns.
                doc = self.get(r[0])
                vec_distance = float(r[-1] or 0.0)
            else:
                d = dict(r)
                vec_distance = float(d.pop("vec_distance", 0.0) or 0.0)
                doc = self._row_to_doc_dict(d)
            score = -vec_distance
            snippet = (doc.body[:120] + "…") if len(doc.body) > 120 else doc.body
            hits.append(SearchHit(doc=doc, snippet=snippet, score=score))

        if tags:
            wanted: set[str] = set(tags)
            hits = [h for h in hits if wanted.issubset(set(h.doc.tags))]
        return hits[:limit]

    # ---- graph expansion (v0.8 特性 #5) --------------------------------------

    def _expand_with_graph(
        self,
        hits: list[SearchHit],
        max_neighbors: int = 5,
        decay: float = 0.6,
    ) -> list[SearchHit]:
        """Enrich each hit with its 1-hop graph neighbors as ``related``.

        For each top hit, performs a single hop outbound + inbound query on
        the ``links`` table, collects up to *max_neighbors* unique neighbors,
        and attaches them as :class:`RelatedDoc` instances with a decayed score.
        """
        if not hits:
            return hits

        doc_ids = [h.doc.id for h in hits]
        # Batch-fetch all neighbor edges in one query per direction
        placeholders = ",".join("?" for _ in doc_ids)

        # Outbound edges
        out_rows = self._conn.execute(
            f"""
            SELECT from_id, to_id, rel FROM links
            WHERE from_id IN ({placeholders})
            """,
            doc_ids,
        ).fetchall()

        # Inbound edges
        in_rows = self._conn.execute(
            f"""
            SELECT to_id AS from_id, from_id AS to_id, rel FROM links
            WHERE to_id IN ({placeholders})
            """,
            doc_ids,
        ).fetchall()

        # Build neighbor map: doc_id → [(neighbor_id, rel, direction)]
        neighbor_map: dict[str, list[tuple[str, str, Literal["outbound", "inbound"]]]] = {
            did: [] for did in doc_ids
        }
        seen_pairs: set[tuple[str, str]] = set()

        for r in out_rows:
            src, dst, rel = r["from_id"], r["to_id"], r["rel"]
            if src in neighbor_map and (src, dst) not in seen_pairs:
                neighbor_map[src].append((dst, rel, "outbound"))
                seen_pairs.add((src, dst))

        for r in in_rows:
            src, dst, rel = r["from_id"], r["to_id"], r["rel"]
            if src in neighbor_map and (src, dst) not in seen_pairs:
                neighbor_map[src].append((dst, rel, "inbound"))
                seen_pairs.add((src, dst))

        # Attach related docs to each hit
        for hit in hits:
            neighbors = neighbor_map.get(hit.doc.id, [])
            related: list[RelatedDoc] = []
            for neighbor_id, rel, direction in neighbors[:max_neighbors]:
                try:
                    neighbor_doc = self.get(neighbor_id)
                    related_score = hit.score * decay
                    related.append(
                        RelatedDoc(
                            doc=neighbor_doc,
                            rel=rel,
                            direction=direction,
                            hop=1,
                            score=round(related_score, 6),
                        )
                    )
                except Exception:
                    pass  # skip missing/deleted docs
            hit.related = related

        return hits

    def _row_to_doc_dict(self, d: dict) -> Document:
        """Convert a plain dict to a Document (after we already materialised a Row).

        Delegates to :meth:`Document.from_row` so JSON-encoded columns
        (``tags``, ``metadata``) are decoded exactly as in :meth:`get`.
        """
        d = dict(d)
        d.pop("snip", None)
        d.pop("score", None)
        return Document.from_row(d)

    @staticmethod
    def _tokenize_cjk_query(query: str) -> list[str]:
        """Split a query into ASCII terms and CJK bi-grams / characters.

        Since SQLite's default unicode61 tokenizer splits on whitespace/punctuation,
        continuous Chinese characters without spaces need character/bi-gram tokenization
        to match trigram or prefix tokens properly.
        """
        import re

        tokens: list[str] = []
        # Match alphanumeric sequences or single CJK characters
        pattern = re.compile(r"([a-zA-Z0-9_\-]+|[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af])")
        matches = pattern.findall(query)

        cjk_buffer: list[str] = []
        for m in matches:
            if re.match(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", m):
                cjk_buffer.append(m)
            else:
                if cjk_buffer:
                    # Output CJK bi-grams and full string
                    if len(cjk_buffer) > 1:
                        for i in range(len(cjk_buffer) - 1):
                            tokens.append(f"{cjk_buffer[i]}{cjk_buffer[i + 1]}")
                    tokens.append("".join(cjk_buffer))
                    cjk_buffer = []
                tokens.append(m)

        if cjk_buffer:
            if len(cjk_buffer) > 1:
                for i in range(len(cjk_buffer) - 1):
                    tokens.append(f"{cjk_buffer[i]}{cjk_buffer[i + 1]}")
            tokens.append("".join(cjk_buffer))

        return [t for t in tokens if t.strip()]

    @classmethod
    def _escape_fts(cls, query: str) -> str:
        """Build an FTS5 expression tolerant to typos, CJK characters and word boundaries."""
        tokens: list[str] = []
        raw_tokens = cls._tokenize_cjk_query(query)
        for tok in raw_tokens:
            tok_clean = tok.strip().replace('"', '""')
            if not tok_clean:
                continue
            if len(tok_clean) >= 3 and tok_clean.isascii():
                tokens.append(f'"{tok_clean}"*')
            else:
                tokens.append(f'"{tok_clean}"')
        return " OR ".join(tokens) if tokens else '""'

    @classmethod
    def _escape_fts_lexical(cls, query: str) -> str:
        """Strict AND-of-tokens FTS5 expression with CJK-aware tokenization."""
        raw_tokens = cls._tokenize_cjk_query(query)
        clean = [t.strip().replace('"', '""') for t in raw_tokens if t.strip()]
        return " AND ".join(f'"{t}"' for t in clean) if clean else '""'


__all__ = ["SearchMixin"]
