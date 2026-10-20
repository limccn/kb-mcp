import subprocess
from pathlib import Path
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.context_guard import ContextGuard, get_git_diff_summary


def test_git_diff_summary_survives_cjk_diff(tmp_path: Path):
    """A CJK diff must survive capture decoding.

    Default text mode on Windows is cp932 and git emits UTF-8; a decode crash in
    the capture reader thread leaves stdout=None, which the blanket ``except`` in
    get_git_diff_summary turns into an empty result — a silent "no
    recommendations" for a real diff.
    """

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)

    git("init", "-q")
    (tmp_path / "note.md").write_text("# 督促状バッチの設計\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.email=e2e@example.com", "-c", "user.name=e2e",
        "commit", "-q", "-m", "init")
    (tmp_path / "note.md").write_text(
        "# 督促状バッチの設計\n\n改訂：一括引抜を追加。\n", encoding="utf-8")

    res = get_git_diff_summary(tmp_path)
    assert res["files"], "dirty diff was silently reported as empty"
    assert "督促状" in res["raw_diff"]


def test_context_guard_evaluation(tmp_path: Path):
    db_path = tmp_path / "guard_kb.db"
    store = SqliteStore(db_path)

    # 1. Add team decisions and lessons
    dec = Document(
        id="dec/use-fastapi",
        type="decision",
        title="Use FastAPI for HTTP APIs",
        body="All backend services must use FastAPI framework and Pydantic models for validation.",
        tags=["framework", "fastapi"],
    )
    lesson = Document(
        id="lesson/dont-block-async-event-loop",
        type="lesson",
        title="Avoid Blocking Sync IO in Async Handlers",
        body="Never call time.sleep() or blocking sqlite queries inside async def endpoints.",
        tags=["async", "fastapi", "performance"],
    )
    store.add(dec)
    store.add(lesson)

    guard = ContextGuard(store=store)

    # Test mock evaluation
    res = guard.evaluate_diff()
    # No git repo in clean test dir returns clean result without crash
    assert "has_recommendations" in res
    assert "prompt_context" in res
