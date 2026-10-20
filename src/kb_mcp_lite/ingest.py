"""``kb ingest``: one folder in, an import-ready Markdown tree out.

Two halves, two homes:

* **Conversion** is delegated to ingestion's batch converter
  (``ingestion/tools/convert_docs.py``, or its frozen sibling
  ``ingestion-rt/doc-convert.exe``) run as a *child process*. kb-mcp.exe
  deliberately excludes ``doc_ingestion`` (``kbmcp/tools/kb-mcp.spec``), so an
  in-process conversion cannot exist in the frozen deployment; a child
  process is the one shape that works both in a venv and in the exe.
* **Import** (only with ``--vault``) is kb's own ``md_io.import_dir`` --
  engine code the exe already carries -- after creating the vault if needed
  (create-first, the ``kb_import.py`` precedent: a bare
  ``kb --vault <new>`` call would otherwise traceback).

Converter discovery: ``--converter`` > ``$KB_INGEST_CONVERTER`` > the dev
checkout (``<repo>/ingestion-rt/doc-convert.exe``, else
``<repo>/ingestion/.venv`` + ``tools/convert_docs.py``). A frozen build has no
checkout layout to fall back to, so deployments pass one of the first two.

Incremental conversion (converter contract v1, 2026-10-14): the converter is
incremental by default -- unchanged files are skipped and outputs of vanished
sources are pruned, so an incremental rerun matches a clean full pass.
``--incremental`` / ``--no-incremental`` are forwarded only when the caller
sets them; the default (``None``) adds neither flag, so a converter predating
the pair keeps working.

Progress seam (task 10-15-kb-admin-ingest-jobs): ``run_converter`` and
``run_ingest`` take two optional keyword-only parameters -- ``report_path``
(a persistent ``--report`` location instead of the throwaway temp directory)
and ``on_progress`` (one callback per complete JSONL record, delivered while
the child still runs). Both default to ``None``, and both-None keeps the
exact original blocking path byte-for-byte; either one switches to a
``Popen`` run whose pipes are drained in threads, plus a report tail thread
when ``on_progress`` is given. The converter writes its report in one burst
at exit today; the tail dispatches complete lines as they appear, so it
works unchanged when a future writer flushes per record.

The child environment drops ``VIRTUAL_ENV``/``PYTHONHOME``/``PYTHONPATH`` and
forces ``PYTHONIOENCODING=utf-8`` (``kb_import.py``'s measured precedent: the
Windows locale encodes a path's Japanese as bytes whose second byte can be
``0x5C`` -- a stray backslash once a JSON string is decoded); stdout/stderr
come back as UTF-8.

Exit-code shape (values applied by ``cli.py``): a provided-but-unusable
converter path is a validation error (2), no converter at all is not-found (3),
a converter that fails without a usable report is internal (5); reported
per-file failures and import errors make the command exit 1 -- "the command
ran" and "everything landed" are different claims (``kb_import.py``'s
measured split).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kb_mcp_lite.md_io import import_dir
from kb_mcp_lite.tree_index import EnrichResult, enrich_tree
from kb_mcp_lite.vault import VaultAlreadyExistsError, VaultManager

__all__ = [
    "ConvertResult",
    "ConverterNotFoundError",
    "ConverterRunError",
    "ConverterValidationError",
    "ImportSummary",
    "IngestError",
    "IngestResult",
    "SkipEntry",
    "find_converter",
    "run_converter",
    "run_ingest",
]


#: Environment variable naming a converter, same rules as ``--converter``.
_CONVERTER_ENV = "KB_INGEST_CONVERTER"

#: Suffixes treated as directly runnable converter paths.
_RUNNABLE_SUFFIXES = {".exe", ".cmd", ".bat", ".com"}

#: %TEMP% prefix for the throwaway --report directory (isolation checks watch it).
_REPORT_PREFIX = "kb-ingest-"

#: The converter's own summary line: ``N converted, M failed, K skipped``,
#: with the incremental contract's optional ``, U unchanged, P pruned`` tail.
_SUMMARY_RE = re.compile(
    r"^(\d+) converted, (\d+) failed, (\d+) skipped"
    r"(?:, (\d+) unchanged)?(?:, (\d+) pruned)?\b",
    re.MULTILINE,
)

#: One ``SKIP <file> -- <reason>`` line (file and reason may hold Japanese).
_SKIP_LINE_RE = re.compile(
    r"^\s*SKIP\s{1,}(?P<file>.+?)\s{1,}--\s{1,}(?P<reason>.+?)\s*$", re.MULTILINE
)

#: Lines a run must not swallow: skips, prunes, and per-file failures.
_NOTABLE_LINE_RE = re.compile(r"^\s*(?:SKIP\b|PRUNE\b|\[[^\]]*\]\s+FAIL\b)")


# ---------------------------------------------------------------------------
# exceptions (mapped to exit codes by cli.py)
# ---------------------------------------------------------------------------


class IngestError(Exception):
    """Base for kb ingest failures the CLI reports by exit code."""


class ConverterValidationError(IngestError):
    """A ``--converter`` / ``$KB_INGEST_CONVERTER`` path is unusable (exit 2)."""


class ConverterNotFoundError(IngestError):
    """No converter could be found at all (exit 3)."""


class ConverterRunError(IngestError):
    """The converter failed in a way that is not a reported per-file failure (exit 5)."""


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkipEntry:
    """One input the converter did not convert, and the reason it gave."""

    file: str
    reason: str


@dataclass
class ConvertResult:
    """Outcome of one converter child process run."""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    converted: int
    failed: int
    skipped: list[SkipEntry]
    unchanged: int
    pruned: int
    notable_lines: list[str]

    @property
    def command_text(self) -> str:
        return " ".join(self.command)


@dataclass
class ImportSummary:
    """The vault-side half of an ingest run."""

    inserted: int
    updated: int
    skipped: int
    errors: list[str]
    vault: str
    created: bool


@dataclass
class IngestResult:
    """Everything ``kb ingest`` has to report: conversion, and import when asked."""

    command: list[str]
    out: Path
    converted: int
    failed: int
    skipped: list[SkipEntry]
    unchanged: int
    pruned: int
    notable_lines: list[str]
    import_summary: ImportSummary | None
    enrich: EnrichResult | None
    returncode: int

    @property
    def command_text(self) -> str:
        return " ".join(self.command)

    def to_dict(self) -> dict[str, Any]:
        """The ``--json`` shape (key set pinned by tests and the e2e suite)."""
        return {
            "converter": self.command_text,
            "out": str(self.out),
            "converted": self.converted,
            "failed": self.failed,
            "skipped": [{"file": s.file, "reason": s.reason} for s in self.skipped],
            "unchanged": self.unchanged,
            "pruned": self.pruned,
            "import": (
                None
                if self.import_summary is None
                else {
                    "inserted": self.import_summary.inserted,
                    "updated": self.import_summary.updated,
                    "skipped": self.import_summary.skipped,
                    "errors": list(self.import_summary.errors),
                    "vault": self.import_summary.vault,
                    "created": self.import_summary.created,
                }
            ),
        }


# ---------------------------------------------------------------------------
# converter discovery
# ---------------------------------------------------------------------------


def _usable(raw: str, source: str) -> list[str]:
    """Validate one provided converter path; return it as a command prefix."""
    path = Path(raw)
    if not path.is_file():
        raise ConverterValidationError(
            f"converter not found at '{raw}' (from {source}); "
            "expected an executable file or a .cmd wrapper"
        )
    if path.suffix.lower() not in _RUNNABLE_SUFFIXES and not os.access(path, os.X_OK):
        raise ConverterValidationError(
            f"converter at '{raw}' (from {source}) is not an executable or .cmd; "
            "expected a directly runnable command"
        )
    return [str(path.resolve())]


def _repo_root() -> Path | None:
    """The dev-checkout root, or None in a frozen build.

    ``kb_mcp_lite`` lives at ``<repo>/kbmcp/src/kb_mcp_lite``, so the checkout
    root is three parents above this file. Under PyInstaller (``sys.frozen``)
    ``__file__`` points into the unpacked bundle -- the layout says nothing
    about the host's disk, so the repo fallback is skipped and deployments are
    expected to pass ``--converter`` or ``KB_INGEST_CONVERTER``.
    """
    if getattr(sys, "frozen", False):
        return None
    return Path(__file__).resolve().parents[3]


def find_converter(explicit: str | None = None) -> list[str]:
    """Locate the conversion command prefix: explicit > env > dev checkout.

    Returns the argv slice to prepend to the converter's ``convert``
    subcommand (a single executable path, or ``[python, script]`` for the
    repo fallback). Raises :class:`ConverterValidationError` for a
    provided-but-unusable path and :class:`ConverterNotFoundError` when
    nothing is available at all.
    """
    if explicit:
        return _usable(explicit, "--converter")
    env_value = os.environ.get(_CONVERTER_ENV, "").strip()
    if env_value:
        return _usable(env_value, _CONVERTER_ENV)
    root = _repo_root()
    if root is not None:
        exe = root / "ingestion-rt" / "doc-convert.exe"
        if exe.is_file():
            return [str(exe)]
        python = root / "ingestion" / ".venv" / "Scripts" / "python.exe"
        script = root / "ingestion" / "tools" / "convert_docs.py"
        if python.is_file() and script.is_file():
            return [str(python), str(script)]
    raise ConverterNotFoundError(
        "no document converter found; provide one of:\n"
        "  --converter PATH            an executable or .cmd wrapper "
        r"(e.g. ingestion-rt\doc-convert.exe)" "\n"
        f"  {_CONVERTER_ENV}=PATH   the same rule, via the environment\n"
        "  a dev checkout carrying <repo>"
        r"\ingestion-rt\doc-convert.exe, or <repo>\ingestion\.venv + tools\convert_docs.py"
    )


# ---------------------------------------------------------------------------
# conversion (child process)
# ---------------------------------------------------------------------------


def _child_env() -> dict[str, str]:
    """Environment for the converter child: no venv pins, UTF-8 output.

    Without ``PYTHONIOENCODING=utf-8`` a child Python writes the Windows
    locale (cp932 here); a Japanese path's bytes can then carry ``0x5C`` and
    stop being plain text (measured in ``kb_import.py``, imported here).
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")
    }
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _tail(stdout: str, stderr: str, limit: int = 800) -> str:
    """The tail of a child process's output, for error messages."""
    text = "\n".join(part.rstrip() for part in (stdout, stderr) if part.strip())
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text or "(no output)"


#: The report's per-record statuses (converter contract v1). A line carrying
#: any other value voids the whole report -- a *newer* writer adding a status
#: can never silently miscount or mis-bucket an older reader.
_REPORT_STATUSES = frozenset({"ok", "failed", "skipped", "unchanged", "pruned"})


def _report_record(line: str) -> dict | None:
    """One JSONL report line -> its record; ``None`` for a blank line.

    The single line-classification both the whole-report parse and the live
    progress tail go through. Raises ``ValueError`` (``json.JSONDecodeError``
    included) for anything that is not a JSON object carrying one of the
    known statuses.
    """
    if not line.strip():
        return None
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError(f"report line is not a JSON object: {line[:100]!r}")
    status = record.get("status")
    if status not in _REPORT_STATUSES:
        raise ValueError(f"unknown report status {status!r}")
    return record


def _fold_report(records: Iterable[dict]) -> tuple[int, int, list[SkipEntry], int, int]:
    """Fold validated records into ``(ok, failed, skipped, unchanged, pruned)``.

    The one status -> counter mapping; the live progress tail in the job
    engine counts through its own row updates, never through a second copy
    of these rules.
    """
    ok = failed = unchanged = pruned = 0
    skipped: list[SkipEntry] = []
    for record in records:
        status = record.get("status")
        if status == "ok":
            ok += 1
        elif status == "failed":
            failed += 1
        elif status == "skipped":
            skipped.append(
                SkipEntry(str(record.get("rel", "")), str(record.get("error", "")))
            )
        elif status == "unchanged":
            unchanged += 1
        elif status == "pruned":
            pruned += 1
        else:
            raise ValueError(f"unknown report status {status!r}")
    return ok, failed, skipped, unchanged, pruned


def _parse_report(path: Path) -> tuple[int, int, list[SkipEntry], int, int] | None:
    """Counts and skips from the converter's ``--report`` JSONL; None if absent.

    The report is the authority when it exists: one JSON object per line with
    ``status`` in ok/failed/skipped (``convert_docs.py``'s record shape, read
    from its code) plus the incremental contract's ``unchanged``/``pruned``
    (counted; never listed among the skips). An unreadable, unparsable, or
    unknown-status report returns None so the caller falls back to the
    independent stdout channel -- unknown status included on purpose, so a
    report from a *newer* writer cannot silently skew the counts.
    """
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        records = [
            record
            for record in (_report_record(line) for line in lines)
            if record is not None
        ]
        return _fold_report(records)
    except (OSError, ValueError):
        return None


def _parse_summary(stdout: str) -> tuple[int, int, list[SkipEntry], int, int] | None:
    """Counts and skips from the converter's stdout summary; None if absent.

    The independent cross-check channel (the suite compares it against the
    report). The skipped count and the named SKIP lines must agree -- a
    summary whose numbers and names disagree is not trusted. The
    ``unchanged``/``pruned`` tail groups are optional: pre-incremental
    summaries carry no tail and read as zero.
    """
    match = _SUMMARY_RE.search(stdout)
    if match is None:
        return None
    converted, failed, named_skipped, unchanged, pruned = (
        int(g) if g is not None else 0 for g in match.groups()
    )
    skipped = [
        SkipEntry(m.group("file"), m.group("reason"))
        for m in _SKIP_LINE_RE.finditer(stdout)
    ]
    if len(skipped) != named_skipped:
        return None
    return converted, failed, skipped, unchanged, pruned


def _notable_lines(stdout: str) -> list[str]:
    """Raw SKIP/FAIL lines, forwarded verbatim -- never dropped in silence."""
    return [line.rstrip() for line in stdout.splitlines() if _NOTABLE_LINE_RE.match(line)]


#: ``mode`` -> the converter's input-form flag. A folder is the default and
#: unchanged; the other two name a single input, whose folder the converter
#: takes as the root -- so all three agree on relative paths, and therefore
#: on output paths and ids.
_INPUT_FLAGS = {"dir": "--dir", "file": "--file", "zip": "--ziped"}


def _convert_argv(
    command: list[str],
    directory: Path,
    out: Path,
    report_path: Path,
    doc_type: str | None,
    incremental: bool | None,
    mode: str,
) -> list[str]:
    """The child argv both run paths share: ``mode`` picks the converter's
    input form; ``--type`` and the incremental flags are appended only when
    the caller gave them (None appends nothing, so a converter predating the
    flag pair stays runnable)."""
    argv = [
        *command,
        "convert",
        _INPUT_FLAGS[mode],
        str(directory),
        "--out",
        str(out),
        "--report",
        str(report_path),
    ]
    if doc_type:
        argv += ["--type", doc_type]
    if incremental is not None:
        argv.append("--incremental" if incremental else "--no-incremental")
    return argv


def _result_from(
    command: list[str],
    returncode: int,
    stdout: str,
    stderr: str,
    parsed: tuple[int, int, list[SkipEntry], int, int],
) -> ConvertResult:
    """The one place a :class:`ConvertResult` is built (shape frozen)."""
    converted, failed, skipped, unchanged, pruned = parsed
    return ConvertResult(
        command=list(command),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        converted=converted,
        failed=failed,
        skipped=skipped,
        unchanged=unchanged,
        pruned=pruned,
        notable_lines=_notable_lines(stdout),
    )


def _run_converter_blocking(
    command: list[str],
    directory: Path,
    out: Path,
    report_path: Path,
    doc_type: str | None,
    incremental: bool | None,
    mode: str,
) -> ConvertResult:
    """The original run: one blocking ``subprocess.run``, both pipes captured."""
    argv = _convert_argv(command, directory, out, report_path, doc_type, incremental, mode)
    proc = subprocess.run(
        argv,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=_child_env(),
    )
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if proc.returncode not in (0, 1):
        raise ConverterRunError(
            f"converter failed with exit code {proc.returncode}; "
            f"output:\n{_tail(stdout, stderr)}"
        )
    parsed = _parse_report(report_path)
    if parsed is None:
        parsed = _parse_summary(stdout)
    if parsed is None:
        raise ConverterRunError(
            "converter wrote no parsable --report and printed no summary line; "
            f"output:\n{_tail(stdout, stderr)}"
        )
    return _result_from(command, proc.returncode, stdout, stderr, parsed)


def _drain_stream(stream: Any, sink: list[str]) -> None:
    """Read one child pipe to EOF into ``sink`` (thread target).

    Both pipes must be drained while the child runs: a full stdout/stderr
    pipe (4096 bytes on Windows) blocks the child at its next write, and a
    parent blocked in ``wait()`` would then wait forever.
    """
    sink.append(stream.read())


def _tail_report_lines(path: Path, offset: int, final: bool) -> tuple[list[str], int]:
    """New report text past byte ``offset`` as lines, plus the new offset.

    While the child runs, only lines that already ended in ``\\n`` are
    returned -- a half-written line waits for the rest of its bytes (the
    split is done on *bytes*, so a partial multibyte character can never
    corrupt a line that is already complete). The final drain (child gone)
    mirrors ``_parse_report``'s ``splitlines()`` handling: a line that died
    without its newline still counts.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return [], offset
    chunk = data[offset:]
    if final:
        return (
            [line.decode("utf-8", errors="replace") for line in chunk.splitlines()],
            len(data),
        )
    parts = chunk.split(b"\n")
    complete, rest = parts[:-1], parts[-1]
    lines = [part.decode("utf-8", errors="replace") for part in complete]
    return lines, len(data) - len(rest)


def _dispatch_records(lines: Iterable[str], on_progress: Callable[[dict], None]) -> bool:
    """Feed complete lines to ``on_progress``; False once the stream is void.

    An invalid line (bad JSON, unknown status) voids the whole stream: the
    callbacks stop and the caller ends on the same report -> summary chain
    the blocking path uses. Already-dispatched records are not recalled --
    they were the stream's own lines; the final counts are authoritative.
    """
    for line in lines:
        try:
            record = _report_record(line)
        except ValueError:
            return False
        if record is not None:
            on_progress(record)
    return True


def _watch_report(
    path: Path, on_progress: Callable[[dict], None], stop: threading.Event
) -> None:
    """Tail the report file in a thread: one poll round per loop until ``stop``.

    ``stop`` is set by the main thread once the child is gone; the loop then
    performs one final drain (half-written tail included) and returns.
    """
    offset = 0
    while True:
        final = stop.is_set()
        lines, offset = _tail_report_lines(path, offset, final)
        if not _dispatch_records(lines, on_progress):
            return
        if final:
            return
        stop.wait(0.1)


def _run_converter_streamed(
    command: list[str],
    directory: Path,
    out: Path,
    report_path: Path,
    doc_type: str | None,
    incremental: bool | None,
    mode: str,
    on_progress: Callable[[dict], None] | None,
) -> ConvertResult:
    """The progress run: ``Popen`` + threaded pipe drains (+ report tail).

    Completion order mirrors the blocking path: the exit code is judged
    first (outside 0/1 -> ``ConverterRunError`` with the output tail), then
    the counts resolve through the same report -> summary chain.
    """
    argv = _convert_argv(command, directory, out, report_path, doc_type, incremental, mode)
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        env=_child_env(),
    )
    out_parts: list[str] = []
    err_parts: list[str] = []
    drains = [
        threading.Thread(target=_drain_stream, args=(proc.stdout, out_parts), daemon=True),
        threading.Thread(target=_drain_stream, args=(proc.stderr, err_parts), daemon=True),
    ]
    for drain in drains:
        drain.start()
    stop = threading.Event()
    tail: threading.Thread | None = None
    if on_progress is not None:
        tail = threading.Thread(
            target=_watch_report, args=(report_path, on_progress, stop), daemon=True
        )
        tail.start()
    try:
        returncode = proc.wait()
    finally:
        stop.set()
        if tail is not None:
            tail.join()
        for drain in drains:
            drain.join()
    stdout = "".join(out_parts)
    stderr = "".join(err_parts)
    if returncode not in (0, 1):
        raise ConverterRunError(
            f"converter failed with exit code {returncode}; "
            f"output:\n{_tail(stdout, stderr)}"
        )
    parsed = _parse_report(report_path)
    if parsed is None:
        parsed = _parse_summary(stdout)
    if parsed is None:
        raise ConverterRunError(
            "converter wrote no parsable --report and printed no summary line; "
            f"output:\n{_tail(stdout, stderr)}"
        )
    return _result_from(command, returncode, stdout, stderr, parsed)


def run_converter(
    command: list[str],
    directory: Path,
    out: Path,
    doc_type: str | None = None,
    incremental: bool | None = None,
    *,
    mode: str = "dir",
    report_path: Path | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> ConvertResult:
    """Run ``<command> convert <flag> <directory> --out <out> --report <report>``.

    ``mode`` picks which converter input form ``directory`` is handed to:
    ``"dir"`` (the whole folder, the default), ``"file"`` (one file) or
    ``"zip"`` (one .zip archive). The parameter keeps its name in all three
    cases because in all three it is the same thing -- the path the
    converter is pointed at -- and the root it derives is its parent for the
    single-input forms.

    ``--type`` and the incremental flags are passed through only when given:
    True/False append ``--incremental``/``--no-incremental`` at the argv tail,
    None appends nothing so the converter's own default applies (and a
    converter predating the flag pair stays runnable).

    ``report_path`` and ``on_progress`` are the progress seam. Both None
    runs exactly the original blocking path with a throwaway report
    directory; either one switches to a ``Popen`` run with drained pipes,
    and ``on_progress`` is called once per complete JSONL record while the
    child still runs. A given ``report_path`` names *this run's* report: a
    stale file at it is removed up front and the report is never deleted.

    A converter exit code outside 0/1, or output that yields neither report
    nor summary, is :class:`ConverterRunError`.
    """
    directory = Path(directory).resolve()
    out = Path(out).resolve()
    if report_path is None and on_progress is None:
        with tempfile.TemporaryDirectory(prefix=_REPORT_PREFIX) as tmp_dir:
            return _run_converter_blocking(
                command, directory, out, Path(tmp_dir) / "report.jsonl",
                doc_type, incremental, mode,
            )
    if report_path is None:
        with tempfile.TemporaryDirectory(prefix=_REPORT_PREFIX) as tmp_dir:
            return _run_converter_streamed(
                command, directory, out, Path(tmp_dir) / "report.jsonl",
                doc_type, incremental, mode, on_progress,
            )
    report = Path(report_path)
    report.unlink(missing_ok=True)  # a stale report must not read as this run's
    return _run_converter_streamed(
        command, directory, out, report, doc_type, incremental, mode, on_progress
    )


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------


def run_ingest(
    directory: Path,
    out: Path,
    *,
    vault_name: str | None = None,
    doc_type: str | None = None,
    incremental: bool | None = None,
    converter: str | None = None,
    vault_manager: VaultManager | None = None,
    mode: str = "dir",
    report_path: Path | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> IngestResult:
    """Convert ``directory`` into ``out``; with ``vault_name``, import it too.

    ``--out`` stays a plain converter tree (no index documents, no graph
    activity); the vault path first grows the tree graph (one ``kb-index``
    document per content directory, see :mod:`kb_mcp_lite.tree_index`),
    then runs create-first, imports, and reports per-file errors. Exit
    code 1 when any file failed to convert, the converter itself exited
    non-zero, or any import error was recorded.

    ``mode`` picks the converter input form (see :func:`run_converter`).
    ``--out``'s tree always mirrors the *input root*, which for the
    single-input modes is the named file's parent -- so the tree graph's
    root title is that folder's name in every mode, never a file name.

    ``incremental`` rides along untouched to :func:`run_converter`: None keeps
    the converter's own default (incremental since contract v1), True/False
    force ``--incremental``/``--no-incremental`` into the child argv.

    ``report_path`` and ``on_progress`` ride along untouched to
    :func:`run_converter` -- the progress seam; both None keeps the whole
    run on the original blocking path.
    """
    directory = Path(directory).resolve()
    out = Path(out).resolve()
    root = directory if mode == "dir" else directory.parent
    command = find_converter(converter)
    convert = run_converter(command, directory, out, doc_type=doc_type,
                            incremental=incremental, mode=mode,
                            report_path=report_path, on_progress=on_progress)

    import_summary: ImportSummary | None = None
    enrich: EnrichResult | None = None
    if vault_name is not None:
        # Tree-graph step (task 10-12-tree-graph-links): per-directory index
        # documents carry the tree as `contains` frontmatter links; they must
        # be on disk before import walks the tree. Only the --vault path
        # grows a graph -- --out stays byte-identical to the converter's.
        enrich = enrich_tree(out, root_title=root.name)
        manager = vault_manager if vault_manager is not None else VaultManager()
        created = True
        try:
            manager.create(vault_name)
        except VaultAlreadyExistsError:
            created = False  # create-first: an existing vault is a no-op
        from kb_mcp_lite.store import SqliteStore

        store = SqliteStore(manager.resolve_path(vault_name))
        try:
            report = import_dir(store, out)
        finally:
            store.close()
        import_summary = ImportSummary(
            inserted=report.inserted,
            updated=report.updated,
            skipped=report.skipped,
            errors=list(report.errors),
            vault=vault_name,
            created=created,
        )

    errors = import_summary.errors if import_summary is not None else []
    returncode = 1 if (convert.failed > 0 or convert.returncode != 0 or errors) else 0
    return IngestResult(
        command=list(command),
        out=out,
        converted=convert.converted,
        failed=convert.failed,
        skipped=list(convert.skipped),
        unchanged=convert.unchanged,
        pruned=convert.pruned,
        notable_lines=list(convert.notable_lines),
        import_summary=import_summary,
        enrich=enrich,
        returncode=returncode,
    )