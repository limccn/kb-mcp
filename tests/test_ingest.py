"""Unit tests for ``kb ingest`` (discovery, conversion parsing, exit codes).

The converter side is exercised with a *fake* converter: a generated ``.cmd``
that wraps a small Python script which writes a tree, a ``--report`` JSONL and
a controllable exit code. No document library is needed here; the real
converter runs in the e2e suite (``kbmcp/e2e/ingest/run.py``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from kb_mcp_lite import ingest as ingest_mod
from kb_mcp_lite.cli import cli
from kb_mcp_lite.ingest import (
    ConverterNotFoundError,
    ConverterRunError,
    ConverterValidationError,
)

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_NOT_FOUND = 3
EXIT_INTERNAL = 5


# ---------------------------------------------------------------------------
# fake converter (generated per test)
# ---------------------------------------------------------------------------

FAKE_HEADER = """# -*- coding: utf-8 -*-
\"\"\"Fake converter stand-in for kb ingest tests: tree + --report + rc.\"\"\"
import json
import os
import sys
from pathlib import Path

RC = {rc}
REPORT_MODE = {report_mode!r}          # 'jsonl' | 'absent' | 'garbage'
PRINT_SUMMARY = {print_summary}
SUMMARY_TAIL = {summary_tail}          # five-count tail (incremental contract v1)
SUMMARY_UNCHANGED = {summary_unchanged}
SUMMARY_PRUNED = {summary_pruned}
PRINT_SKIP_LINE = {print_skip_line}
PRINT_PRUNE_LINE = {print_prune_line}
PRINT_FAIL_LINE = {print_fail_line}
SKIPPED_IN_SUMMARY = {skipped_in_summary}
SUMMARY_CONVERTED = {summary_converted}
FAILED_IN_REPORT = {failed_in_report}
OK_IN_REPORT = {ok_in_report}
UNCHANGED_IN_REPORT = {unchanged_in_report}
PRUNED_IN_REPORT = {pruned_in_report}
UNKNOWN_IN_REPORT = {unknown_in_report}
REJECT_INCREMENTAL_FLAGS = {reject_incremental_flags}
"""

FAKE_BODY = '''
argv = sys.argv

# An old converter (pre-incremental-contract) rejects flags it does not know,
# the way argparse does: rc 2 plus a usage line naming the offending flag.
if REJECT_INCREMENTAL_FLAGS and any(
    a in ("--incremental", "--no-incremental") for a in argv
):
    print(
        "usage: fake_convert.py convert ... error: unrecognized arguments: "
        + " ".join(a for a in argv if a in ("--incremental", "--no-incremental")),
        file=sys.stderr,
    )
    sys.exit(2)


def arg(name):
    return argv[argv.index(name) + 1] if name in argv else None


out = Path(arg("--out"))
out.mkdir(parents=True, exist_ok=True)
(out / "doc-a.md").write_text(
    "---\\nid: doc-a\\ntype: spec\\ntitle: Doc A\\n---\\n\\nfake body\\n",
    encoding="utf-8",
)
(out / "argv.json").write_text(json.dumps(argv[1:], ensure_ascii=False), encoding="utf-8")
(out / "env.json").write_text(
    json.dumps(
        {
            "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV"),
            "PYTHONHOME": os.environ.get("PYTHONHOME"),
            "PYTHONPATH": os.environ.get("PYTHONPATH"),
            "PYTHONIOENCODING": os.environ.get("PYTHONIOENCODING"),
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)

skip_file = "帳票マクロ.xlsm"
skip_reason = "fake skip for tests"
print("scanning " + str(arg("--dir")))
print("  1 to convert, 1 to skip (2 files total)")
print("[  1/1] OK    txt    doc-a.txt -> doc-a.md (10 chars, id doc-a)")
if PRINT_SKIP_LINE:
    print("      SKIP  " + skip_file + " -- " + skip_reason)
if PRINT_PRUNE_LINE:
    print("      PRUNE gone.md  (source gone: gone.txt)")
if PRINT_FAIL_LINE:
    print("[  2/2] FAIL  broken.docx -- ConversionError: boom")
if PRINT_SUMMARY:
    tail = ""
    if SUMMARY_TAIL:
        tail = ", {} unchanged, {} pruned".format(SUMMARY_UNCHANGED, SUMMARY_PRUNED)
    print(
        "{} converted, 0 failed, {} skipped".format(
            SUMMARY_CONVERTED, SKIPPED_IN_SUMMARY
        )
        + tail
        + " -> "
        + str(out)
    )

report = arg("--report")
if report and REPORT_MODE != "absent":
    records = []
    for n in range(OK_IN_REPORT):
        records.append(
            {
                "rel": "doc-{}.txt".format(n),
                "kind": "txt",
                "status": "ok",
                "out": "doc-{}.md".format(n),
                "id": "doc-{}".format(n),
                "chars": 10,
                "converter": "fake",
                "lost": {},
                "warnings": [],
                "error": "",
            }
        )
    for n in range(UNCHANGED_IN_REPORT):
        records.append(
            {
                "rel": "kept-{}.txt".format(n),
                "kind": "txt",
                "status": "unchanged",
                "out": "kept-{}.md".format(n),
                "id": "kept-{}".format(n),
                "chars": 0,
                "converter": "",
                "lost": {},
                "warnings": [],
                "error": "",
            }
        )
    for n in range(PRUNED_IN_REPORT):
        records.append(
            {
                "rel": "gone-{}.txt".format(n),
                "kind": "txt",
                "status": "pruned",
                "out": "gone-{}.md".format(n),
                "id": "gone-{}".format(n),
                "chars": 0,
                "converter": "",
                "lost": {},
                "warnings": [],
                "error": "source gone",
            }
        )
    if FAILED_IN_REPORT:
        records.append(
            {
                "rel": "broken.docx",
                "kind": "docx",
                "status": "failed",
                "out": "broken.md",
                "id": "",
                "chars": 0,
                "converter": "",
                "lost": {},
                "warnings": [],
                "error": "ConversionError: boom",
            }
        )
    records.append(
        {
            "rel": skip_file,
            "kind": "skip",
            "status": "skipped",
            "out": "",
            "id": "",
            "chars": 0,
            "converter": "",
            "lost": {},
            "warnings": [],
            "error": skip_reason,
        }
    )
    if UNKNOWN_IN_REPORT:
        records.append(
            {
                "rel": "new-0.txt",
                "kind": "txt",
                "status": "flushed",
                "out": "new-0.md",
                "id": "new-0",
                "chars": 0,
                "converter": "",
                "lost": {},
                "warnings": [],
                "error": "",
            }
        )
    text = "\\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\\n"
    if REPORT_MODE == "garbage":
        text = "not json at all\\n"
    Path(report).parent.mkdir(parents=True, exist_ok=True)
    Path(report).write_text(text, encoding="utf-8")

sys.exit(RC)
'''


def make_fake(
    tmp_path: Path,
    *,
    rc: int = 0,
    report_mode: str = "jsonl",
    print_summary: bool = True,
    summary_tail: bool = False,
    summary_unchanged: int = 0,
    summary_pruned: int = 0,
    print_skip_line: bool = True,
    print_prune_line: bool = False,
    print_fail_line: bool = False,
    skipped_in_summary: int = 1,
    summary_converted: int = 1,
    failed_in_report: bool = False,
    ok_in_report: int = 1,
    unchanged_in_report: int = 0,
    pruned_in_report: int = 0,
    unknown_in_report: bool = False,
    reject_incremental_flags: bool = False,
) -> Path:
    """Generate a fake converter .cmd; returns its path."""
    fake_dir = tmp_path / ("fake-{:03d}".format(make_fake.counter))
    make_fake.counter += 1  # type: ignore[attr-defined]
    fake_dir.mkdir()
    script = fake_dir / "fake_convert.py"
    script.write_text(
        FAKE_HEADER.format(
            rc=rc,
            report_mode=report_mode,
            print_summary=print_summary,
            summary_tail=summary_tail,
            summary_unchanged=summary_unchanged,
            summary_pruned=summary_pruned,
            print_skip_line=print_skip_line,
            print_prune_line=print_prune_line,
            print_fail_line=print_fail_line,
            skipped_in_summary=skipped_in_summary,
            summary_converted=summary_converted,
            failed_in_report=failed_in_report,
            ok_in_report=ok_in_report,
            unchanged_in_report=unchanged_in_report,
            pruned_in_report=pruned_in_report,
            unknown_in_report=unknown_in_report,
            reject_incremental_flags=reject_incremental_flags,
        )
        + FAKE_BODY,
        encoding="utf-8",
    )
    cmd = fake_dir / "fake-convert.cmd"
    # ASCII + CRLF on purpose: the console codepage here is 932, and either
    # one alone desyncs CMD's line reader (repo convention).
    cmd.write_text(
        "@echo off\r\n"
        '"{}" "%~dp0fake_convert.py" %*\r\n'.format(sys.executable)
        + "exit /b %ERRORLEVEL%\r\n",
        encoding="ascii",
    )
    return cmd


make_fake.counter = 0  # type: ignore[attr-defined]


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def kb_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every CLI invocation must land in a throwaway home, never the real one."""
    home = tmp_path / "kbhome"
    monkeypatch.setenv("KB_MCP_HOME", str(home))
    monkeypatch.delenv("KB_INGEST_CONVERTER", raising=False)
    return home


def full(result: Result) -> str:
    """stdout+stderr: click 8.4 keeps them apart, the message may be in either."""
    return result.output + (getattr(result, "stderr", "") or "")


def invoke(runner: CliRunner, args: list[str]) -> Result:
    return runner.invoke(cli, args)


# ---------------------------------------------------------------------------
# find_converter — the discovery matrix (flag > env > repo fallback)
# ---------------------------------------------------------------------------


class TestFindConverter:
    def test_flag_wins_over_env(self, tmp_path, monkeypatch) -> None:
        flag = make_fake(tmp_path)
        env = make_fake(tmp_path)
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(env))
        assert ingest_mod.find_converter(str(flag)) == [str(flag.resolve())]

    def test_env_wins_over_repo_fallback(self, tmp_path, monkeypatch) -> None:
        env = make_fake(tmp_path)
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(env))
        root = tmp_path / "repo"
        (root / "ingestion-rt").mkdir(parents=True)
        (root / "ingestion-rt" / "doc-convert.exe").write_bytes(b"MZ")
        monkeypatch.setattr(ingest_mod, "_repo_root", lambda: root)
        assert ingest_mod.find_converter(None) == [str(env.resolve())]

    def test_repo_fallback_prefers_frozen_exe(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("KB_INGEST_CONVERTER", raising=False)
        root = tmp_path / "repo"
        (root / "ingestion-rt").mkdir(parents=True)
        exe = root / "ingestion-rt" / "doc-convert.exe"
        exe.write_bytes(b"MZ")
        (root / "ingestion" / ".venv" / "Scripts").mkdir(parents=True)
        (root / "ingestion" / ".venv" / "Scripts" / "python.exe").write_bytes(b"MZ")
        (root / "ingestion" / "tools").mkdir(parents=True)
        (root / "ingestion" / "tools" / "convert_docs.py").write_text("x")
        monkeypatch.setattr(ingest_mod, "_repo_root", lambda: root)
        assert ingest_mod.find_converter(None) == [str(exe)]

    def test_repo_fallback_venv_combo(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("KB_INGEST_CONVERTER", raising=False)
        root = tmp_path / "repo"
        python = root / "ingestion" / ".venv" / "Scripts" / "python.exe"
        script = root / "ingestion" / "tools" / "convert_docs.py"
        python.parent.mkdir(parents=True, exist_ok=True)
        script.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"MZ")
        script.write_text("# converter\n")
        monkeypatch.setattr(ingest_mod, "_repo_root", lambda: root)
        assert ingest_mod.find_converter(None) == [str(python), str(script)]

    def test_frozen_build_has_no_repo_fallback(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("KB_INGEST_CONVERTER", raising=False)
        # This checkout *does* carry a converter (ingestion-rt), so a
        # non-frozen find_converter() would succeed in this very test
        # process; the frozen guard must hide the repo fallback even so.
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        with pytest.raises(ConverterNotFoundError):
            ingest_mod.find_converter(None)

    def test_nothing_found_lists_all_three_options(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("KB_INGEST_CONVERTER", raising=False)
        empty = tmp_path / "empty-repo"
        empty.mkdir()
        monkeypatch.setattr(ingest_mod, "_repo_root", lambda: empty)
        with pytest.raises(ConverterNotFoundError) as excinfo:
            ingest_mod.find_converter(None)
        message = str(excinfo.value)
        assert "--converter" in message
        assert "KB_INGEST_CONVERTER" in message
        assert "doc-convert.exe" in message
        assert "convert_docs.py" in message

    def test_explicit_missing_path_is_validation(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("KB_INGEST_CONVERTER", raising=False)
        missing = tmp_path / "no-such-converter.exe"
        with pytest.raises(ConverterValidationError) as excinfo:
            ingest_mod.find_converter(str(missing))
        assert str(missing) in str(excinfo.value)
        assert "--converter" in str(excinfo.value)

    def test_env_missing_path_is_validation(self, tmp_path, monkeypatch) -> None:
        missing = tmp_path / "no-such-converter.cmd"
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(missing))
        with pytest.raises(ConverterValidationError) as excinfo:
            ingest_mod.find_converter(None)
        assert str(missing) in str(excinfo.value)
        assert "KB_INGEST_CONVERTER" in str(excinfo.value)


# ---------------------------------------------------------------------------
# run_converter — report parsing, stdout fallback, rc mapping, env hygiene
# ---------------------------------------------------------------------------


class TestRunConverter:
    def run_fake(self, tmp_path, fake, doc_type=None):
        return ingest_mod.run_converter(
            [str(fake)], tmp_path / "src", tmp_path / "out", doc_type=doc_type
        )

    def test_report_is_the_authority(self, tmp_path) -> None:
        fake = make_fake(tmp_path, ok_in_report=2, summary_converted=1)
        result = self.run_fake(tmp_path, fake)
        assert result.converted == 2  # report count, not the stdout "1"
        assert result.failed == 0
        assert [s.file for s in result.skipped] == ["帳票マクロ.xlsm"]
        assert result.skipped[0].reason == "fake skip for tests"
        assert result.returncode == 0

    def test_stdout_fallback_when_report_absent(self, tmp_path) -> None:
        fake = make_fake(tmp_path, report_mode="absent")
        result = self.run_fake(tmp_path, fake)
        assert (result.converted, result.failed) == (1, 0)
        assert [s.file for s in result.skipped] == ["帳票マクロ.xlsm"]
        assert any("SKIP" in line and "帳票マクロ.xlsm" in line
                   for line in result.notable_lines)

    def test_garbage_report_falls_back_to_stdout(self, tmp_path) -> None:
        fake = make_fake(tmp_path, report_mode="garbage")
        result = self.run_fake(tmp_path, fake)
        assert (result.converted, result.failed) == (1, 0)

    def test_unknown_report_status_falls_back_to_stdout(self, tmp_path) -> None:
        # Fallback completeness: a *newer* writer may add statuses this reader
        # does not know ('flushed'). The whole report is then untrusted --
        # counts come from the stdout summary, never a silent miscount. The
        # two channels are distinguishable here: the report claims 5
        # converted, the summary says 1.
        fake = make_fake(tmp_path, ok_in_report=5, unknown_in_report=True)
        result = self.run_fake(tmp_path, fake)
        assert (result.converted, result.failed) == (1, 0)
        assert (result.unchanged, result.pruned) == (0, 0)
        assert [s.file for s in result.skipped] == ["帳票マクロ.xlsm"]

    def test_summary_with_disagreeing_names_is_not_trusted(self, tmp_path) -> None:
        fake = make_fake(tmp_path, report_mode="absent", print_skip_line=False)
        with pytest.raises(ConverterRunError):
            self.run_fake(tmp_path, fake)

    def test_nothing_parsable_is_internal_error(self, tmp_path) -> None:
        fake = make_fake(
            tmp_path, report_mode="absent", print_summary=False, print_skip_line=False
        )
        with pytest.raises(ConverterRunError) as excinfo:
            self.run_fake(tmp_path, fake)
        assert "no parsable --report" in str(excinfo.value)

    def test_converter_rc2_is_internal_error(self, tmp_path) -> None:
        fake = make_fake(tmp_path, rc=2)
        with pytest.raises(ConverterRunError) as excinfo:
            self.run_fake(tmp_path, fake)
        message = str(excinfo.value)
        assert "exit code 2" in message
        assert "scanning" in message  # the converter's own output follows

    def test_failed_record_maps_to_rc1_but_still_returns(self, tmp_path) -> None:
        fake = make_fake(tmp_path, failed_in_report=True)
        result = self.run_fake(tmp_path, fake)
        assert result.returncode == 0  # the *process* succeeded
        assert result.failed == 1

    def test_command_line_shape(self, tmp_path) -> None:
        fake = make_fake(tmp_path)
        self.run_fake(tmp_path, fake, doc_type="spec")
        argv = json.loads((tmp_path / "out" / "argv.json").read_text("utf-8"))
        assert argv[0] == "convert"
        assert argv[argv.index("--dir") + 1] == str((tmp_path / "src").resolve())
        assert argv[argv.index("--out") + 1] == str((tmp_path / "out").resolve())
        assert argv[argv.index("--type") + 1] == "spec"
        assert "--report" in argv

        fake2 = make_fake(tmp_path)
        ingest_mod.run_converter([str(fake2)], tmp_path / "src", tmp_path / "out2")
        argv2 = json.loads((tmp_path / "out2" / "argv.json").read_text("utf-8"))
        assert "--type" not in argv2  # converter default applies otherwise

    def test_child_env_strips_venv_pins_and_forces_utf8(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("VIRTUAL_ENV", r"C:\some\venv")
        monkeypatch.setenv("PYTHONHOME", r"C:\some\home")
        monkeypatch.setenv("PYTHONPATH", r"C:\some\path")
        monkeypatch.setenv("PYTHONIOENCODING", "cp932")
        fake = make_fake(tmp_path)
        self.run_fake(tmp_path, fake)
        probe = json.loads((tmp_path / "out" / "env.json").read_text("utf-8"))
        assert probe["VIRTUAL_ENV"] is None
        assert probe["PYTHONHOME"] is None
        assert probe["PYTHONPATH"] is None
        assert probe["PYTHONIOENCODING"] == "utf-8"

    def test_notable_lines_forward_skips_and_failures(self, tmp_path) -> None:
        fake = make_fake(tmp_path, print_fail_line=True)
        result = self.run_fake(tmp_path, fake)
        joined = "\n".join(result.notable_lines)
        assert "帳票マクロ.xlsm" in joined
        assert "broken.docx" in joined
        assert "FAIL" in joined

    def test_incremental_flag_tri_state(self, tmp_path) -> None:
        # K1: None adds no flag (old converters keep working); True/False
        # append the flag at the argv tail, after any --type pair.
        fake = make_fake(tmp_path)

        def argv_of(name: str) -> list[str]:
            return json.loads((tmp_path / name / "argv.json").read_text("utf-8"))

        ingest_mod.run_converter([str(fake)], tmp_path / "src", tmp_path / "out-default")
        default = argv_of("out-default")
        assert "--incremental" not in default
        assert "--no-incremental" not in default

        ingest_mod.run_converter([str(fake)], tmp_path / "src", tmp_path / "out-on",
                                 doc_type="spec", incremental=True)
        on = argv_of("out-on")
        assert on[-1] == "--incremental"
        assert on[-2] == "spec"  # appended *after* the --type pair
        assert "--no-incremental" not in on

        ingest_mod.run_converter([str(fake)], tmp_path / "src", tmp_path / "out-off",
                                 incremental=False)
        off = argv_of("out-off")
        assert off[-1] == "--no-incremental"
        assert "--incremental" not in off

    def test_report_counts_unchanged_and_pruned(self, tmp_path) -> None:
        # K2: the report is the authority; unchanged/pruned are counted and
        # never listed among the skips.
        fake = make_fake(tmp_path, ok_in_report=1, unchanged_in_report=2,
                         pruned_in_report=1)
        result = self.run_fake(tmp_path, fake)
        assert (result.converted, result.failed) == (1, 0)
        assert result.unchanged == 2
        assert result.pruned == 1
        assert [s.file for s in result.skipped] == ["帳票マクロ.xlsm"]

    def test_summary_tail_groups_parse(self, tmp_path) -> None:
        # K3: the summary's optional ", U unchanged, P pruned" tail.
        fake = make_fake(tmp_path, report_mode="absent", summary_tail=True,
                         summary_unchanged=2, summary_pruned=1)
        result = self.run_fake(tmp_path, fake)
        assert (result.converted, result.failed) == (1, 0)
        assert (result.unchanged, result.pruned) == (2, 1)
        assert [s.file for s in result.skipped] == ["帳票マクロ.xlsm"]

    def test_summary_tail_still_cross_checks_named_skips(self, tmp_path) -> None:
        # K3: the tail must not weaken the SKIP-names cross-check.
        fake = make_fake(tmp_path, report_mode="absent", summary_tail=True,
                         summary_unchanged=2, summary_pruned=1, print_skip_line=False)
        with pytest.raises(ConverterRunError):
            self.run_fake(tmp_path, fake)

    def test_prune_lines_are_notable(self, tmp_path) -> None:
        # K4: PRUNE lines are forwarded verbatim, like SKIP/FAIL.
        fake = make_fake(tmp_path, print_prune_line=True)
        result = self.run_fake(tmp_path, fake)
        assert any(ln == "      PRUNE gone.md  (source gone: gone.txt)"
                   for ln in result.notable_lines), result.notable_lines


# ---------------------------------------------------------------------------
# kb ingest through the CLI — JSON shape, exit codes, human output
# ---------------------------------------------------------------------------


class TestCli:
    def test_json_shape_pure_conversion(self, tmp_path, runner, kb_home, monkeypatch) -> None:
        fake = make_fake(tmp_path)
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(fake))
        src = tmp_path / "src"
        src.mkdir()
        result = invoke(runner, ["ingest", str(src), "--out", str(tmp_path / "tree"), "--json"])
        assert result.exit_code == EXIT_OK, full(result)
        payload = json.loads(result.output)
        assert set(payload) == {
            "converter", "out", "converted", "failed", "skipped", "unchanged",
            "pruned", "import",
        }
        assert payload["converter"] == str(fake.resolve())
        assert payload["out"] == str((tmp_path / "tree").resolve())
        assert payload["converted"] == 1
        assert payload["failed"] == 0
        assert payload["unchanged"] == 0
        assert payload["pruned"] == 0
        assert payload["skipped"] == [
            {"file": "帳票マクロ.xlsm", "reason": "fake skip for tests"}
        ]
        assert payload["import"] is None

    def test_json_with_vault_create_first(self, tmp_path, runner, kb_home, monkeypatch) -> None:
        fake = make_fake(tmp_path)
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(fake))
        src = tmp_path / "src"
        src.mkdir()
        args = ["ingest", str(src), "--out", str(tmp_path / "tree"), "--vault", "v1", "--json"]
        first = invoke(runner, args)
        assert first.exit_code == EXIT_OK, full(first)
        payload = json.loads(first.output)
        assert set(payload["import"]) == {
            "inserted", "updated", "skipped", "errors", "vault", "created"
        }
        assert payload["import"]["created"] is True
        # 1 converted doc + 1 generated kb-index document (tree graph).
        assert payload["import"]["inserted"] == 2
        assert payload["import"]["errors"] == []

        second = invoke(runner, args)
        assert second.exit_code == EXIT_OK, full(second)
        payload2 = json.loads(second.output)
        assert payload2["import"]["created"] is False
        assert (payload2["import"]["inserted"], payload2["import"]["updated"]) == (0, 2)

        # The vault carries the tree graph: doc-a plus the generated index.
        listed = invoke(runner, ["--vault", "v1", "list", "--json"])
        assert listed.exit_code == EXIT_OK, full(listed)
        ids = {d["id"] for d in json.loads(listed.output)}
        assert ids == {"doc-a", "kb-index"}

    def test_human_output(self, tmp_path, runner, kb_home, monkeypatch) -> None:
        fake = make_fake(tmp_path)
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(fake))
        src = tmp_path / "src"
        src.mkdir()
        result = invoke(
            runner,
            ["ingest", str(src), "--out", str(tmp_path / "tree"), "--vault", "v1"],
        )
        assert result.exit_code == EXIT_OK, full(result)
        out = full(result)
        assert "1 converted, 0 failed, 1 skipped, 0 unchanged, 0 pruned" in out
        assert "SKIP  帳票マクロ.xlsm -- fake skip for tests" in out
        assert f"tree: {(tmp_path / 'tree').resolve()}" in out
        assert "vault: v1 (created)" in out
        assert "tree index: 1 generated, 0 skipped, 1 edges" in out
        assert "Imported 2 files: 2 inserted, 0 updated" in out

    def test_exit_1_when_a_file_failed(self, tmp_path, runner, kb_home, monkeypatch) -> None:
        fake = make_fake(tmp_path, failed_in_report=True, print_fail_line=True)
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(fake))
        src = tmp_path / "src"
        src.mkdir()
        result = invoke(runner, ["ingest", str(src), "--out", str(tmp_path / "tree"), "--json"])
        assert result.exit_code == 1, full(result)
        assert json.loads(result.output)["failed"] == 1

    def test_exit_5_on_converter_rc2(self, tmp_path, runner, kb_home, monkeypatch) -> None:
        fake = make_fake(tmp_path, rc=2)
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(fake))
        src = tmp_path / "src"
        src.mkdir()
        result = invoke(runner, ["ingest", str(src), "--out", str(tmp_path / "tree")])
        assert result.exit_code == EXIT_INTERNAL, full(result)
        assert "exit code 2" in full(result)

    def test_exit_2_on_bad_explicit_converter(self, tmp_path, runner, kb_home) -> None:
        src = tmp_path / "src"
        src.mkdir()
        missing = tmp_path / "no-such.exe"
        result = invoke(
            runner,
            ["ingest", str(src), "--out", str(tmp_path / "tree"), "--converter", str(missing)],
        )
        assert result.exit_code == EXIT_VALIDATION, full(result)
        assert str(missing) in full(result)

    def test_exit_2_on_bad_env_converter(self, tmp_path, runner, kb_home, monkeypatch) -> None:
        src = tmp_path / "src"
        src.mkdir()
        missing = tmp_path / "no-such.cmd"
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(missing))
        result = invoke(runner, ["ingest", str(src), "--out", str(tmp_path / "tree")])
        assert result.exit_code == EXIT_VALIDATION, full(result)
        assert "KB_INGEST_CONVERTER" in full(result)

    def test_exit_3_when_no_converter_at_all(self, tmp_path, runner, kb_home, monkeypatch) -> None:
        empty = tmp_path / "empty-repo"
        empty.mkdir()
        monkeypatch.setattr(ingest_mod, "_repo_root", lambda: empty)
        src = tmp_path / "src"
        src.mkdir()
        result = invoke(runner, ["ingest", str(src), "--out", str(tmp_path / "tree")])
        assert result.exit_code == EXIT_NOT_FOUND, full(result)
        message = full(result)
        for needle in ("--converter", "KB_INGEST_CONVERTER", "doc-convert.exe"):
            assert needle in message

    def test_incremental_flag_passthrough(self, tmp_path, runner, kb_home, monkeypatch) -> None:
        # K1 at the CLI level: the click flag pair's tri-state reaches the
        # child argv (default None -> no flag; --incremental/--no-incremental
        # -> that flag at the tail).
        fake = make_fake(tmp_path)
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(fake))
        src = tmp_path / "src"
        src.mkdir()
        for args, flag in (
            ([], None),
            (["--incremental"], "--incremental"),
            (["--no-incremental"], "--no-incremental"),
        ):
            tree = tmp_path / ("tree-" + (flag or "default").lstrip("-"))
            result = invoke(runner, ["ingest", str(src), "--out", str(tree), *args])
            assert result.exit_code == EXIT_OK, full(result)
            argv = json.loads((tree / "argv.json").read_text("utf-8"))
            if flag is None:
                assert "--incremental" not in argv
                assert "--no-incremental" not in argv
            else:
                assert argv[-1] == flag

    def test_explicit_flag_against_flagless_converter_is_rc5(
        self, tmp_path, runner, kb_home, monkeypatch
    ) -> None:
        # Version skew (parent design §2.8): a *new* kb run with an explicit
        # flag against a converter that predates the pair (argparse rejects
        # it, rc 2) must fail diagnosably -- rc 5 carrying the converter's
        # own tail -- never a silently wrong mode. Default (no flag) still
        # runs against the same old converter.
        fake = make_fake(tmp_path, reject_incremental_flags=True)
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(fake))
        src = tmp_path / "src"
        src.mkdir()
        ok = invoke(runner, ["ingest", str(src), "--out", str(tmp_path / "tree-old")])
        assert ok.exit_code == EXIT_OK, full(ok)

        bad = invoke(runner, ["ingest", str(src), "--out", str(tmp_path / "tree-bad"),
                              "--no-incremental"])
        assert bad.exit_code == EXIT_INTERNAL, full(bad)
        assert "unrecognized arguments: --no-incremental" in full(bad)

    def test_help_is_readable(self, runner, kb_home) -> None:
        result = invoke(runner, ["ingest", "--help"])
        assert result.exit_code == EXIT_OK
        assert "--out" in result.output
        assert "--converter" in result.output
        assert "--no-incremental" in result.output


# ---------------------------------------------------------------------------
# run_ingest — vault create-first without a CLI
# ---------------------------------------------------------------------------


class TestRunIngestVault:
    def test_create_first_then_existing(self, tmp_path, monkeypatch) -> None:
        from kb_mcp_lite.vault import VaultManager

        monkeypatch.delenv("KB_INGEST_CONVERTER", raising=False)
        fake = make_fake(tmp_path)
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(fake))
        src = tmp_path / "src"
        src.mkdir()
        vm = VaultManager(tmp_path / "home")

        first = ingest_mod.run_ingest(src, tmp_path / "tree", vault_name="v1", vault_manager=vm)
        assert first.import_summary is not None
        assert first.import_summary.created is True
        # 1 converted doc + 1 generated kb-index document (tree graph).
        assert first.import_summary.inserted == 2
        assert first.import_summary.errors == []
        assert first.returncode == 0
        assert first.enrich is not None
        assert first.enrich.generated == 1
        assert first.enrich.edges == 1
        assert (tmp_path / "home" / "v1" / "kb.db").is_file()

        second = ingest_mod.run_ingest(src, tmp_path / "tree", vault_name="v1", vault_manager=vm)
        assert second.import_summary is not None
        assert second.import_summary.created is False
        assert (second.import_summary.inserted, second.import_summary.updated) == (0, 2)

    def test_import_errors_force_rc1(self, tmp_path, monkeypatch) -> None:
        # The fake's tree is a valid doc, so a real import error needs a
        # second run of a *different* source: overwrite the tree file so the
        # same id arrives under a second source (id conflict -> skipped+error).
        from kb_mcp_lite.vault import VaultManager

        monkeypatch.delenv("KB_INGEST_CONVERTER", raising=False)
        fake = make_fake(tmp_path)
        monkeypatch.setenv("KB_INGEST_CONVERTER", str(fake))
        src = tmp_path / "src"
        src.mkdir()
        vm = VaultManager(tmp_path / "home")
        tree = tmp_path / "tree"

        first = ingest_mod.run_ingest(src, tree, vault_name="v1", vault_manager=vm)
        assert first.returncode == 0

        # A second tree with the same document id but a different source
        # (id conflict; no source match) is skipped with an error.
        tree2 = tmp_path / "tree2"
        tree2.mkdir()
        (tree2 / "other.md").write_text(
            "---\nid: doc-a\ntype: spec\ntitle: Doc A again\n---\n\nother body\n",
            encoding="utf-8",
        )
        second = ingest_mod.run_ingest(src, tree2, vault_name="v1", vault_manager=vm)
        assert second.import_summary is not None
        assert second.import_summary.skipped == 1
        assert second.import_summary.errors
        assert second.returncode == 1