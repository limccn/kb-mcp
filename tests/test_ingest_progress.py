"""Unit tests for the ``run_converter`` / ``run_ingest`` progress seam (C2/S1).

The seam has one hard promise -- *both new parameters None keeps the exact
original blocking path* -- and one delivery promise: with ``on_progress``,
complete report records are handed to the callback while the converter child
still runs. Both are pinned here against a generated stub converter
(``.cmd`` + Python pair, ASCII + CRLF as the repo convention demands): the
scenario is chosen by ``$KB_PROG_SCENARIO``, which rides into the child
through ``_child_env``'s pass-through.

Guards use spies, not a poisoned ``subprocess.Popen``: ``subprocess.run``
itself resolves the module-global ``Popen``, so raising from there would
red the *default* path too and prove nothing. Instead the blocking path is
proven by spying ``subprocess.run`` (it must be the one that runs), and the
streamed path by poisoning ``subprocess.run`` (it must never run).
"""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from kb_mcp_lite import ingest as ingest_mod
from kb_mcp_lite.ingest import (
    ConvertResult,
    ConverterRunError,
    run_converter,
    run_ingest,
)

# ---------------------------------------------------------------------------
# stub converter (.cmd + .py, generated once per test)
# ---------------------------------------------------------------------------

STUB_SOURCE = '''\
"""Progress-seam stub converter; scenario chosen by $KB_PROG_SCENARIO.

Reads --out / --report from argv, replays one report-writing scenario, and
prints a summary line the way the real converter does.
"""
import json
import os
import sys
import time
from pathlib import Path

SCENARIO = os.environ["KB_PROG_SCENARIO"]
argv = sys.argv


def arg(name):
    return argv[argv.index(name) + 1] if name in argv else None


out = Path(arg("--out"))
out.mkdir(parents=True, exist_ok=True)
report = Path(arg("--report"))
report.parent.mkdir(parents=True, exist_ok=True)


def record(rel, status="ok", error=""):
    stem = rel.rsplit(".", 1)[0]
    return {
        "rel": rel,
        "kind": "txt",
        "status": status,
        "out": stem + ".md",
        "id": stem,
        "chars": 10,
        "converter": "stub",
        "lost": {},
        "warnings": [],
        "error": error,
    }


def emit(lines):
    with open(report, "a", encoding="utf-8") as sink:
        for line in lines:
            sink.write(json.dumps(line, ensure_ascii=False) + "\\n")
        sink.flush()


def summary(converted):
    print("%d converted, 0 failed, 0 skipped" % converted)


if SCENARIO == "report":
    emit([record("a.txt"), record("b.txt")])
    summary(2)
elif SCENARIO == "lines":
    emit([record("a.txt")])
    time.sleep(0.4)
    emit([record("b.txt"), record("c.txt")])
    time.sleep(2.0)
    summary(3)
elif SCENARIO == "partial":
    emit([record("a.txt")])
    line = json.dumps(record("b.txt"), ensure_ascii=False) + "\\n"
    half = len(line) // 2
    with open(report, "a", encoding="utf-8") as sink:
        sink.write(line[:half])
        sink.flush()
    time.sleep(0.4)
    with open(report, "a", encoding="utf-8") as sink:
        sink.write(line[half:])
        sink.flush()
    emit([record("c.txt")])
    summary(3)
elif SCENARIO == "noeol":
    emit([record("a.txt")])
    with open(report, "a", encoding="utf-8") as sink:
        sink.write(json.dumps(record("b.txt"), ensure_ascii=False))
        sink.flush()
    summary(2)
elif SCENARIO == "garbage":
    emit([record("a.txt")])
    with open(report, "a", encoding="utf-8") as sink:
        sink.write("this is not json at all\\n")
        sink.flush()
    emit([record("b.txt")])
    summary(5)
elif SCENARIO == "unknown":
    emit([record("a.txt")])
    emit([record("b.txt", status="flushed")])
    emit([record("c.txt")])
    summary(5)
elif SCENARIO == "arrayline":
    emit([record("a.txt")])
    with open(report, "a", encoding="utf-8") as sink:
        sink.write("[1, 2, 3]\\n")  # valid JSON, but not a record object
        sink.flush()
    emit([record("b.txt")])
    summary(5)
elif SCENARIO == "absent":
    summary(5)
elif SCENARIO == "flood":
    for _ in range(25):
        sys.stderr.write("e" * 8192)
    sys.stderr.flush()
    summary(5)
elif SCENARIO == "rc2":
    emit([record("a.txt")])
    sys.stderr.write("stub refused to finish\\n")
    sys.exit(2)
else:
    raise SystemExit("unknown scenario " + SCENARIO)

sys.exit(0)
'''


def make_stub(tmp_path: Path) -> Path:
    """Generate the stub converter; returns its ``.cmd`` path."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    script = stub_dir / "progress_stub.py"
    script.write_text(STUB_SOURCE, encoding="utf-8")
    cmd = stub_dir / "progress-stub.cmd"
    # ASCII + CRLF on purpose: the console codepage here is 932, and either
    # one alone desyncs CMD's line reader (repo convention).
    cmd.write_text(
        "@echo off\r\n"
        '"{}" "%~dp0progress_stub.py" %*\r\n'.format(sys.executable)
        + "exit /b %ERRORLEVEL%\r\n",
        encoding="ascii",
    )
    return cmd


@pytest.fixture()
def converter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(cmd, src dir) with ``KB_INGEST_CONVERTER`` pointed at the stub."""
    cmd = make_stub(tmp_path)
    monkeypatch.setenv("KB_INGEST_CONVERTER", str(cmd))
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    return cmd, src


def collector() -> tuple[list[tuple[float, str]], object]:
    """(events, callback): every record as ``(monotonic time, rel)``."""
    events: list[tuple[float, str]] = []

    def on_progress(record: dict) -> None:
        events.append((time.monotonic(), record["rel"]))

    return events, on_progress


def run_with_deadline(fn, timeout: float = 20.0):
    """Run ``fn`` on a daemon thread; fail loudly if it does not finish in
    ``timeout`` (the shape a blocked pipe deadlock takes)."""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # re-raised in the test thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"call did not finish within {timeout}s (deadlock?)"
    if "error" in box:
        raise box["error"]
    return box["value"]


# ---------------------------------------------------------------------------
# the frozen result shape
# ---------------------------------------------------------------------------


def test_convert_result_fields_frozen() -> None:
    assert [f.name for f in dataclasses.fields(ConvertResult)] == [
        "command",
        "returncode",
        "stdout",
        "stderr",
        "converted",
        "failed",
        "skipped",
        "unchanged",
        "pruned",
        "notable_lines",
    ]


# ---------------------------------------------------------------------------
# the gate: both None == the original blocking path, either one == Popen
# ---------------------------------------------------------------------------


def test_default_params_stay_on_blocking_run(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "report")
    real_run = ingest_mod.subprocess.run
    calls: list[dict] = []

    def spy(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return real_run(argv, **kwargs)

    monkeypatch.setattr(ingest_mod.subprocess, "run", spy)
    result = run_converter([str(cmd)], src, tmp_path / "out")

    assert len(calls) == 1  # the blocking path is the one that ran
    assert calls[0]["kwargs"].get("capture_output") is True
    assert result.converted == 2


def test_streamed_path_never_calls_blocking_run(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "report")

    def poison(*args, **kwargs):
        raise AssertionError("the streamed path must not call subprocess.run")

    monkeypatch.setattr(ingest_mod.subprocess, "run", poison)
    events, on_progress = collector()
    result = run_converter(
        [str(cmd)], src, tmp_path / "out", on_progress=on_progress
    )

    assert result.converted == 2
    assert [rel for _, rel in events] == ["a.txt", "b.txt"]


# ---------------------------------------------------------------------------
# on_progress: complete records, in order, while the child still runs
# ---------------------------------------------------------------------------


def test_on_progress_delivers_while_child_runs(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "lines")
    events, on_progress = collector()

    start = time.monotonic()
    result = run_converter(
        [str(cmd)], src, tmp_path / "out", on_progress=on_progress
    )
    returned = time.monotonic()

    assert [rel for _, rel in events] == ["a.txt", "b.txt", "c.txt"]
    # b,c were written 0.4s after a: their callback cannot have been batched
    # with a's, and it arrived well before the child exited (2.0s later).
    assert events[1][0] - events[0][0] >= 0.2
    assert returned - events[2][0] >= 1.0
    assert result.converted == 3


def test_partial_line_waits_for_its_newline(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "partial")
    events, on_progress = collector()

    result = run_converter(
        [str(cmd)], src, tmp_path / "out", on_progress=on_progress
    )

    # All three arrived -- the half-written b line was held back until its
    # newline showed up (dispatching it early would void the stream), and
    # the hold costs the 0.4s the stub sleeps.
    assert [rel for _, rel in events] == ["a.txt", "b.txt", "c.txt"]
    assert events[1][0] - events[0][0] >= 0.2
    assert result.converted == 3


def test_final_drain_takes_a_line_without_newline(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "noeol")
    events, on_progress = collector()

    result = run_converter(
        [str(cmd)], src, tmp_path / "out", on_progress=on_progress
    )

    # The last record died without its newline: the final drain reads it the
    # way splitlines() does -- one more record, not one fewer.
    assert [rel for _, rel in events] == ["a.txt", "b.txt"]
    assert result.converted == 2


# ---------------------------------------------------------------------------
# voiding: a bad line stops callbacks and voids the report
# ---------------------------------------------------------------------------


def test_garbage_line_voids_stream_and_report(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "garbage")
    events, on_progress = collector()

    result = run_converter(
        [str(cmd)], src, tmp_path / "out", on_progress=on_progress
    )

    # Callbacks stop at the garbage: only the record before it arrived.
    assert [rel for _, rel in events] == ["a.txt"]
    # The unusable report is voided wholesale; the summary wins.
    assert result.converted == 5
    assert result.failed == 0 and result.skipped == []


def test_unknown_status_voids_stream_and_report(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "unknown")
    events, on_progress = collector()

    result = run_converter(
        [str(cmd)], src, tmp_path / "out", on_progress=on_progress
    )

    assert [rel for _, rel in events] == ["a.txt"]
    assert result.converted == 5  # from the summary, not the voided report


def test_non_object_json_line_voids_stream_and_report(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "arrayline")
    events, on_progress = collector()

    result = run_converter(
        [str(cmd)], src, tmp_path / "out", on_progress=on_progress
    )

    # A line that parses as JSON but is not an object is an invalid line like
    # any other: the classifier must raise ValueError (its documented
    # contract), so the tail voids the stream instead of dying on
    # AttributeError and the report falls back to the summary wholesale.
    assert [rel for _, rel in events] == ["a.txt"]
    assert result.converted == 5
    assert result.failed == 0 and result.skipped == []


# ---------------------------------------------------------------------------
# exit codes and output capture on the streamed path
# ---------------------------------------------------------------------------


def test_exit_code_2_is_converter_run_error(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "rc2")

    with pytest.raises(ConverterRunError) as excinfo:
        run_converter(
            [str(cmd)], src, tmp_path / "out", on_progress=lambda record: None
        )
    assert "exit code 2" in str(excinfo.value)
    assert "stub refused to finish" in str(excinfo.value)


def test_stderr_flood_does_not_deadlock(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "flood")
    events, on_progress = collector()

    # 200 KiB through a 4 KiB pipe: without the stderr drain thread the
    # child would block mid-write and the run would never return.
    result = run_with_deadline(
        lambda: run_converter(
            [str(cmd)], src, tmp_path / "out", on_progress=on_progress
        )
    )

    assert result.stderr.count("e") == 25 * 8192  # every byte came back
    assert result.converted == 5
    assert events == []


# ---------------------------------------------------------------------------
# report_path: named location, kept on disk, stale file never mistaken
# ---------------------------------------------------------------------------


def test_summary_only_run_with_on_progress(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "absent")
    events, on_progress = collector()

    result = run_converter(
        [str(cmd)], src, tmp_path / "out", on_progress=on_progress
    )

    assert events == []
    assert result.converted == 5  # summary is the independent channel


def test_report_path_alone_keeps_and_counts_from_report(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "report")
    report = tmp_path / "kept-report.jsonl"

    result = run_converter([str(cmd)], src, tmp_path / "out", report_path=report)

    assert result.converted == 2
    assert result.returncode == 0
    lines = report.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["rel"] for line in lines] == ["a.txt", "b.txt"]


def test_stale_report_at_report_path_is_discarded(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "absent")  # writes no report
    stale = tmp_path / "stale-report.jsonl"
    stale.write_text(
        json.dumps(
            {
                "rel": "old.txt",
                "kind": "txt",
                "status": "ok",
                "out": "old.md",
                "id": "old",
                "chars": 1,
                "converter": "old",
                "lost": {},
                "warnings": [],
                "error": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_converter(
        [str(cmd)], src, tmp_path / "out", report_path=stale,
        on_progress=lambda record: None,
    )

    assert result.converted == 5  # the summary, not the stale "1 ok"
    assert not stale.exists()  # removed up front, never rewritten


# ---------------------------------------------------------------------------
# run_ingest: the passthrough seam
# ---------------------------------------------------------------------------


def test_run_ingest_defaults_stay_blocking(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "report")
    real_run = ingest_mod.subprocess.run
    calls: list[list[str]] = []

    def spy(argv, **kwargs):
        calls.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(ingest_mod.subprocess, "run", spy)
    result = run_ingest(src, tmp_path / "out")

    assert len(calls) == 1
    assert result.converted == 2


def test_run_ingest_forwards_progress_seam(
    converter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cmd, src = converter
    monkeypatch.setenv("KB_PROG_SCENARIO", "report")
    events, on_progress = collector()
    report = tmp_path / "ingest-report.jsonl"

    result = run_ingest(
        src, tmp_path / "out", report_path=report, on_progress=on_progress
    )

    assert [rel for _, rel in events] == ["a.txt", "b.txt"]
    assert result.converted == 2
    assert report.is_file()
    assert len(report.read_text(encoding="utf-8").splitlines()) == 2