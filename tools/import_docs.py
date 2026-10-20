#!/usr/bin/env python
"""Ingest a folder into one knowledge base (vault).

Generalises ``testdoc-import/import.py``: the source directory and the target
vault are arguments here, not the three hard-coded corpora.

Per file:

    .md / .markdown  copied into the staging tree as-is -- the file's own
                     frontmatter governs its type, title, id and tags
    .docx .pptx .xlsx .xlsm .xls .pdf .txt .json
                     converted by kb_mcp_lite.docconvert (the doc_ingestion
                     wheel installed by tools\\build-install.cmd)
    .doc             antiword -m UTF-8.txt; the doc-ingestion wheel has no
                     .doc reader, so this one stays a separate path
    anything else    skipped, and named in the summary

Every ingestible file is staged into ONE tree and imported with ONE
``kb --vault <vault> import <staging> --json`` call: one process, one report.

The staged relative path is what ``kb`` records as the document's ``source``
(``md_io.import_dir`` passes it itself and ignores any frontmatter ``source``),
so the layout is deterministic -- ``converted/<relative path>.md`` for
generated documents, the original relative path for copied ones -- and a
re-run updates those same documents instead of importing copies. That makes
``converted/`` a reserved name: a folder of the user's own called that is
skipped (and said so), because a copy there would take a generated document's
staged path and rewrite it on the next run.

Generated documents carry an explicit ASCII ``id``. That is not decoration:
``kb import`` derives a document's id from its path when frontmatter gives
none, and the id validator (``^[a-z0-9][a-z0-9/_-]*$``) rejects Japanese
filenames outright -- ``帳票レイアウト仕様.md`` fails with a pydantic error.
This tool sidesteps that by never letting the path be the id.

``kb import`` exits 0 even when a file fails to parse; the failures arrive in
the JSON report's ``errors`` list. This tool reads that list and exits
non-zero -- "the command succeeded" and "the documents are in" are different
claims, and only the second one is what this runs for.

Run (import-docs.cmd does this): the build-install venv's interpreter, which
holds both the installed adapter and pyyaml:

    <package>\\dist\\venv\\Scripts\\python.exe import_docs.py \\
          --dir <folder> --vault <name> --kb <path to kb.exe>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

import yaml

from kb_mcp_lite.docconvert import SUFFIXES, convert

COPY_SUFFIXES = {".md", ".markdown"}
# Only .doc is converted in-process; every other type goes through
# kb_mcp_lite.docconvert, which reports the converter it actually used per
# file (meta.converter).
CONVERTERS = {".doc": "antiword -m UTF-8.txt"}
CONVERTED_PREFIX = "converted"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9/_-]*$")


class Item(NamedTuple):
    rel: str  # path relative to --dir, posix separators
    kind: str  # "copy" | a converter suffix stem ("docx", "pdf", ...) | "skip"
    reason: str  # why it was skipped, "" otherwise


# ---------------------------------------------------------------------------
# child process environment
# ---------------------------------------------------------------------------


def child_env() -> dict[str, str]:
    """Environment for ``kb``: no inherited venv, UTF-8 on the way out.

    PYTHONIOENCODING is not optional. Without it the child writes the Windows
    locale (cp932), whose second bytes can be 0x5C -- a bare backslash inside
    a JSON string, so ``--json`` stops being parseable exactly when a path
    holds Japanese, i.e. on the documents this tool exists for.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(cmd: list[str]) -> bytes:
    """Run a converter, turning any failure into one readable line."""
    try:
        r = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError as e:
        raise RuntimeError(f"{cmd[0]} not found on PATH ({e.strerror})") from e
    if r.returncode != 0:
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"{cmd[0]} exited {r.returncode}: "
                           f"{(tail[-1] if tail else 'no stderr')[:200]}")
    return r.stdout


# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------


def slug_id(stem: str) -> str:
    """An id matching the validator, for a stem that may be entirely Japanese.

    Mirrors ``schema.slugify``: ASCII is kept, everything else collapses to
    ``-``, and a stem with no usable ASCII falls back to ``cjk-<sha1[:8]>``
    instead of an empty slug (which is what used to make every CJK document
    of one type collide). Deterministic: same stem, same id.
    """
    ascii_part = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:40].strip("-")
    if ascii_part and ID_RE.match(ascii_part):
        return ascii_part
    return "cjk-" + hashlib.sha1(stem.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def extract_doc(src: Path) -> tuple[str, dict]:
    # -m UTF-8.txt is mandatory: antiword's default mapping turns Japanese
    # into '????' and the loss is silent.
    out = _run(["antiword", "-m", "UTF-8.txt", str(src)])
    return out.decode("utf-8", "replace"), {
        "converter": CONVERTERS[".doc"], "lost": {}, "warnings": []}


# Every suffix the adapter advertises, plus .doc (antiword). The suffix list
# is imported rather than retyped so the two cannot drift apart; the adapter
# owns the doc_ingestion seam (dispatch, temp bridge, ConversionError, and
# the lost/warnings counts).
#
# Copy suffixes are filtered out: the adapter now knows .md too, but a .md
# under here is still a *copy* (classify checks COPY_SUFFIXES first, and that
# is on purpose -- a bare .md is kb's own format and must not be rewritten).
# Leaving it in would put a .md entry in this table that nothing can ever
# reach, and a table that lists an unreachable row is a lie about what runs.
EXTRACTORS = {suffix: convert for suffix in SUFFIXES
              if suffix not in COPY_SUFFIXES}
EXTRACTORS[".doc"] = extract_doc


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def classify(rel: str) -> Item:
    # Checked before the suffixes, not after: this subtree is where generated
    # documents live, and a copied .md there would land on a generated one's
    # staged path -- same source, so the next re-import would rewrite that
    # document with the wrong file's text. The whole subtree is left alone.
    if rel.startswith(CONVERTED_PREFIX + "/"):
        return Item(rel, "skip", f"lives under '{CONVERTED_PREFIX}/', reserved for "
                                 f"generated documents")
    suffix = Path(rel).suffix.lower()
    if suffix in COPY_SUFFIXES:
        return Item(rel, "copy", "")
    if suffix in EXTRACTORS:
        return Item(rel, suffix.lstrip("."), "")
    return Item(rel, "skip", f"no extractor for {suffix or 'an extensionless file'}")


def plan(root: Path) -> list[Item]:
    """Every file under ``root``, classified, in a deterministic order.

    Hidden files and hidden directories are left out -- ``kb import`` skips
    them without a word, and nothing should be silently absent from the
    summary either.
    """
    items: list[Item] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        base = Path(dirpath)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            items.append(classify((base / name).relative_to(root).as_posix()))
    return items


def staged_path(item: Item, used: set[str]) -> str:
    """Where ``item`` lands in the staging tree. Deterministic, unique.

    Copied Markdown keeps its relative path, with the suffix normalised to
    ``.md`` (``kb`` matches ``.md`` literally and would drop ``.markdown`` or
    ``.MD``). Generated Markdown goes under ``converted/`` so the two
    namespaces cannot overwrite each other on a re-import.
    """
    if item.kind == "copy":
        cand = Path(item.rel).with_suffix(".md").as_posix()
    else:
        cand = f"{CONVERTED_PREFIX}/{Path(item.rel).with_suffix('').as_posix()}.md"
    final, n = cand, 1
    while final in used:
        n += 1
        final = f"{cand[:-3]}-{n}.md"
    used.add(final)
    return final


def build_markdown(item: Item, title: str, body: str, meta: dict,
                   doc_id: str, doc_type: str, vault: str) -> str:
    metadata = {
        "original": item.rel,
        "extractor": meta["converter"],
        "chars": len(body),
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    # What the converter saw but did not turn into Markdown, and what it
    # warned about: written down only when there is something to say.
    if meta.get("lost"):
        metadata["lost"] = meta["lost"]
    if meta.get("warnings"):
        metadata["warnings"] = meta["warnings"]
    fm = {
        "id": doc_id,
        "type": doc_type,
        "title": title,
        "tags": [vault],
        "metadata": metadata,
    }
    head = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    # The Excel and JSON converters head their output with an H1 of their
    # own; adding "# {title}" on top would publish two H1s in one document.
    own_heading = "" if body.startswith("# ") else f"# {title}\n\n"
    return (f"---\n{head}---\n\n{own_heading}"
            f"> Extracted from `{item.rel}` ({meta['converter']}).\n\n{body}\n")


# ---------------------------------------------------------------------------
# kb
# ---------------------------------------------------------------------------


def kb_run(kb_exe: Path, args: list[str], timeout: int) -> tuple[int, str]:
    r = subprocess.run([str(kb_exe), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=child_env(),
                       timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class Stager:
    """The staging tree, and the mapping back to the files it came from."""

    def __init__(self, staging: Path) -> None:
        self.root = staging.resolve()
        self.used: set[str] = set()
        self.used_ids: set[str] = set()
        self.back: dict[str, str] = {}  # staged absolute path -> original rel

    def place(self, item: Item) -> str:
        rel = staged_path(item, self.used)
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        self.back[str(path)] = item.rel
        return rel

    def reserve_id(self, staged_rel: str) -> None:
        """Claim the id ``kb`` will derive from a copied file's staged path.

        Copied files carry no explicit id -- the path is the id -- so a
        generated document must not pick one of those, or kb rejects it with
        "document already exists" and a whole run reads as failed.
        """
        self.used_ids.add(Path(staged_rel).with_suffix("").as_posix())

    def unique_id(self, stem: str) -> str:
        base, n, doc_id = slug_id(stem), 1, ""
        doc_id = base
        while doc_id in self.used_ids:
            n += 1
            doc_id = f"{base}-{n}"
        self.used_ids.add(doc_id)
        return doc_id

    def readable(self, err: str) -> str:
        """Rewrite kb's staging path back to the file the user pointed at."""
        path_part, sep, rest = err.partition(": ")
        return f"{self.back.get(path_part, path_part)}{sep}{rest}"


def main() -> int:
    # The console codepage on Windows is 932; everything printed here can be
    # Japanese. Reconfiguring is the difference between readable and mojibake.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True, help="Folder to scan (recursively).")
    ap.add_argument("--vault", required=True, help="Target vault name.")
    ap.add_argument("--kb", required=True, help="Path to kb.exe (the installed CLI).")
    ap.add_argument("--type", default="spec",
                    help="Document type for generated documents (default: spec). "
                         "Copied .md files keep their own frontmatter type.")
    ap.add_argument("--timeout", type=int, default=900,
                    help="Seconds allowed for the kb import call (default: 900).")
    ap.add_argument("--staging", default="",
                    help="Reuse this directory instead of a fresh temp one. The "
                         "staged paths are what kb records as each document's "
                         "source, so reusing another folder's tree rewrites "
                         "documents instead of importing them.")
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    kb_exe = Path(args.kb).resolve()
    if not root.is_dir():
        print(f"[X] not a directory: {root}")
        return 2
    if not kb_exe.exists():
        print(f"[X] kb.exe not found: {kb_exe}")
        print("    Run tools\\build-install.cmd first, or pass --kb.")
        return 2

    items = plan(root)
    ingestible = [i for i in items if i.kind != "skip"]
    skipped = [i for i in items if i.kind == "skip"]
    print(f"scanning {root}")
    print(f"  {len(ingestible)} to ingest, {len(skipped)} to skip "
          f"({len(items)} files total)")
    if not ingestible:
        print("[X] nothing to ingest -- no .md/.markdown/.doc/.docx/.pptx/"
              ".xlsx/.xlsm/.xls/.pdf/.txt/.json here.")
        for i in skipped[:10]:
            print(f"      SKIP  {i.rel} -- {i.reason}")
        if len(skipped) > 10:
            print(f"      ... and {len(skipped) - 10} more skipped")
        return 2

    staging = Path(args.staging) if args.staging else Path(
        tempfile.mkdtemp(prefix="kb-import-docs-"))
    staging.mkdir(parents=True, exist_ok=True)
    stager = Stager(staging)

    # The user's own files go first: they keep their natural staged path and
    # their path-derived id, and a generated document yields to them (a-2,
    # converted/a-2.md) rather than colliding with one.
    ordered = sorted(ingestible, key=lambda i: i.kind != "copy")
    ok = failed = 0
    for n, item in enumerate(ordered, 1):
        tag = f"[{n:>3}/{len(ingestible)}]"
        try:
            rel = stager.place(item)
            dest = stager.root / rel
            if item.kind == "copy":
                shutil.copy2(root / item.rel, dest)
                stager.reserve_id(rel)
                print(f"{tag} OK    copy  {item.rel}")
            else:
                body, meta = EXTRACTORS[Path(item.rel).suffix.lower()](root / item.rel)
                title = Path(item.rel).stem
                doc_id = stager.unique_id(title)
                dest.write_text(build_markdown(item, title, body, meta, doc_id,
                                               args.type, args.vault),
                                encoding="utf-8")
                print(f"{tag} OK    {item.kind:<4}  {item.rel} -> {doc_id} "
                      f"({len(body)} chars)")
            ok += 1
        except Exception as exc:  # noqa: BLE001 - recorded, never silent
            failed += 1
            print(f"{tag} FAIL  {item.rel} -- {type(exc).__name__}: {exc}")

    for i in skipped:
        print(f"      SKIP  {i.rel} -- {i.reason}")

    import_errors: list[str] = []
    if ok:
        print(f"\nkb --vault {args.vault} import <staging> --json")
        rc, out = kb_run(kb_exe, ["--vault", args.vault, "import", str(stager.root),
                                  "--json"], args.timeout)
        report: dict = {}
        try:
            report = json.loads(out[out.index("{"):])
        except (ValueError, KeyError):
            print(f"[X] kb import returned no JSON report (exit {rc}):")
            print("    " + out.strip()[:600])
            return finish(stager, root, args.vault, ok, failed, len(skipped), 1)
        print(f"  inserted {report.get('inserted')}, updated {report.get('updated')}, "
              f"skipped by kb {report.get('skipped')}")
        import_errors = [stager.readable(e) for e in (report.get("errors") or [])]
        if import_errors:
            print(f"  [X] {len(import_errors)} file(s) rejected by kb import:")
            for e in import_errors:
                # The first line carries the file; pydantic's explanation
                # continues on later lines and needs to stay attached.
                first, *rest = e.splitlines()
                print(f"      - {first}")
                for line in rest:
                    print(f"        {line}")

    return finish(stager, root, args.vault, ok, failed, len(skipped), len(import_errors))


def finish(stager: Stager, root: Path, vault: str, ok: int, failed: int,
           skipped: int, rejected: int) -> int:
    bad = failed + rejected
    print("-" * 60)
    print(f"vault {vault}: {ok} staged, {skipped} skipped, {bad} failed")
    if bad:
        # A clean run's staging tree is disposable; a failed one is evidence.
        print(f"staging kept for inspection: {stager.root}")
        print(f"source folder: {root}")
        return 1
    shutil.rmtree(stager.root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())