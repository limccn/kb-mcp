"""Document to Markdown adapter: one path in, ``(markdown, meta)`` out.

This module is the single seam between kbmcp and the converters in the
``doc_ingestion`` package (a separate wheel, installed by
``tools\\build-install.cmd``). It does no conversion itself -- it dispatches
by extension, bridges the file-based converters through a throwaway temp
file, folds their failure modes into one named exception, and reports what
each converter could not turn into Markdown.

Two deliberate choices:

- **No ``ingestion()`` unified entry.** That entry writes chunk JSON, needs a
  work dir, and its dispatch table historically missed extensions; the
  adapter calls each converter entry point directly, so the only artifact is
  the Markdown string. Chunking stays kb's job.
- **Lazy imports.** ``doc_ingestion`` is not a dependency of kbmcp: importing
  this module (and ``import kb_mcp_lite``) must work where it is absent. The
  first ``convert()`` call that needs it fails with a named
  :class:`ConversionError` pointing at the installer instead.

:: code-block:: python

    from kb_mcp_lite.docconvert import convert, ConversionError

    md, meta = convert("report.docx")
    # meta == {"converter": "python-docx",
    #          "lost": {"images": 2},        # only non-zero counts
    #          "warnings": []}

``convert`` never returns an empty string: an unsupported extension, a
missing file, a converter failure, or empty output all raise
:class:`ConversionError` whose message names the file and the reason.
"""

from __future__ import annotations

import importlib
import json
import tempfile
from pathlib import Path

__all__ = ["SUFFIXES", "ConversionError", "convert"]

#: Extension -> converter family. ``.doc`` is deliberately absent: the
#: doc-ingestion wheel has no .doc reader, so the ingest pipeline keeps
#: antiword for it (a separate path in tools/import_docs.py). ``.zip`` is
#: absent for a different reason: an archive is a container, not a format,
#: and unpacking it belongs to the batch converter (ingestion's
#: convert_docs.py), not to this one-path-in seam -- a ``.zip`` handed here
#: is refused by name like any other unsupported extension.
SUFFIXES: dict[str, str] = {
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "excel",
    ".xlsm": "excel",
    ".xls": "excel",
    ".pdf": "pdf",
    ".txt": "txt",
    ".json": "json",
    ".md": "md",
    ".csv": "csv",
}


class ConversionError(Exception):
    """A conversion that could not produce Markdown. Message names file + reason."""


def _load(module_name: str, src: Path):
    """Import one doc_ingestion module on first use, or refuse by name.

    The ``ImportError`` message is kept so a broken install (a missing
    dependency inside doc_ingestion) is distinguishable from a missing one.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise ConversionError(
            f"{src.name}: doc_ingestion is not installed ({exc}); "
            "run tools\\build-install.cmd first"
        ) from exc


def _docx(src: Path) -> tuple[str, dict, str, list[str]]:
    module = _load("doc_ingestion.ingestion_docx.parse_docx", src)
    with tempfile.TemporaryDirectory(prefix="kb-docconvert-") as tmp:
        out = Path(tmp) / "out.md"
        ok, _heading_map, stats = module.parse_docx_to_markdown(
            str(src), str(out),
            resolve_numbering=True, extract_textbox=True, skip_sections=None)
        if not ok:
            raise ConversionError(f"{src.name}: docx conversion failed "
                                  "(unreadable file or empty output)")
        return out.read_text("utf-8"), stats, "python-docx", []


def _pptx(src: Path) -> tuple[str, dict, str, list[str]]:
    # The package __init__ only exports run(); the function lives in the submodule.
    module = _load("doc_ingestion.ingestion_pptx_v2.parse_pptx", src)
    with tempfile.TemporaryDirectory(prefix="kb-docconvert-") as tmp:
        out = Path(tmp) / "out.md"
        ok, stats = module.parse_pptx(str(src), str(out))
        if not ok:
            raise ConversionError(f"{src.name}: pptx conversion failed "
                                  "(unreadable file or empty output)")
        return out.read_text("utf-8"), stats, "python-pptx", []


def _excel(src: Path) -> tuple[str, dict, str, list[str]]:
    module = _load("doc_ingestion.ingestion_xlsx.excel_parser.core", src)
    markdown = module.ExcelToMarkdownConverter().convert_to_markdown(src)
    converter = "xlrd" if src.suffix.lower() == ".xls" else "openpyxl"
    return markdown, {}, converter, []


def _pdf(src: Path) -> tuple[str, dict, str, list[str]]:
    module = _load("doc_ingestion.ingestion_pdf.parse_pdf", src)
    with tempfile.TemporaryDirectory(prefix="kb-docconvert-") as tmp:
        out = Path(tmp) / "out.md"
        ok, _heading_map = module.parse_pdf_to_markdown(str(src), str(out))
        if not ok:
            raise ConversionError(f"{src.name}: pdf conversion failed "
                                  "(unreadable file or empty output)")
        return out.read_text("utf-8"), {}, "pymupdf", []


def _txt(src: Path) -> tuple[str, dict, str, list[str]]:
    module = _load("doc_ingestion.ingestion_txt.parse_txt", src)
    return module.parse_txt(str(src)), {}, "builtin", []


def _json(src: Path) -> tuple[str, dict, str, list[str]]:
    module = _load("doc_ingestion.ingestion_json.pipeline", src)
    with tempfile.TemporaryDirectory(prefix="kb-docconvert-") as tmp:
        out = Path(tmp) / "out.md"
        chunk = Path(tmp) / "chunks.json"
        module.run(str(src), tmp, str(out), str(chunk))
        warnings = json.loads(chunk.read_text("utf-8")).get("warnings") or []
        return out.read_text("utf-8"), {}, "builtin", warnings


def _md(src: Path) -> tuple[str, dict, str, list[str]]:
    """Markdown passthrough: the body is the document. Only a leading
    frontmatter block is dropped -- keeping it would leave two blocks in
    the output and kb reads only the first."""
    module = _load("doc_ingestion.ingestion_md.parse_md", src)
    body, stats = module.parse_md(str(src))
    return body, stats, "builtin", []


def _csv(src: Path) -> tuple[str, dict, str, list[str]]:
    """CSV -> GitHub-flavoured Markdown table, comma-separated."""
    module = _load("doc_ingestion.ingestion_csv.parse_csv", src)
    return module.parse_csv(str(src)), {}, "builtin", []


_SEAMS = {
    "docx": _docx,
    "pptx": _pptx,
    "excel": _excel,
    "pdf": _pdf,
    "txt": _txt,
    "json": _json,
    "md": _md,
    "csv": _csv,
}


def convert(path: str | Path) -> tuple[str, dict]:
    """Convert one document to Markdown.

    Returns ``(markdown, meta)`` where ``meta`` has fixed keys:

    ``converter``
        the implementation that did the work (``python-docx``, ``xlrd``,
        ``pymupdf``, ...).
    ``lost``
        counts of elements the converter saw but did not turn into Markdown
        (images, charts, headers/footers, ...); only non-zero keys, so
        ``{}`` means "nothing to report" rather than "not counted".
    ``warnings``
        converter warnings, e.g. the JSON pipeline's oversize-chunk notes.

    Raises :class:`ConversionError` for an unsupported extension, a missing
    file, a converter failure, or empty output -- never returns ``""``.
    """
    src = Path(path)
    if not src.is_file():
        raise ConversionError(f"{src.name}: file not found ({src})")
    kind = SUFFIXES.get(src.suffix.lower())
    if kind is None:
        supported = ", ".join(sorted(SUFFIXES))
        raise ConversionError(
            f"{src.name}: unsupported extension {src.suffix or '(none)'} "
            f"(supported: {supported})")

    try:
        markdown, stats, converter, warnings = _SEAMS[kind](src)
    except ConversionError:
        raise
    except Exception as exc:  # noqa: BLE001 - one contract out of many failure modes
        raise ConversionError(f"{src.name}: {type(exc).__name__}: {exc}") from exc

    if not markdown.strip():
        raise ConversionError(f"{src.name}: converter produced empty markdown")

    meta = {
        "converter": converter,
        "lost": {key: count for key, count in stats.items() if count > 0},
        "warnings": list(warnings),
    }
    return markdown, meta