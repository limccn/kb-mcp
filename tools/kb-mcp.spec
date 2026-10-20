# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: the ``kb-mcp`` onefile console exe.

Build (driven by ``build-exe.cmd``, run from the kbmcp project root)::

    dist\\exe-venv\\Scripts\\python.exe -m PyInstaller --clean -y \\
        --workpath dist\\exe-work --distpath dist\\exe tools\\kb-mcp.spec

The exe carries the whole kb engine (CLI + MCP stdio/http + admin +
watcher/scheduler) and deliberately does NOT carry doc_ingestion: document
conversion stays in the repo-side venv workflow (user decision 2026-10-09),
and ``kb_mcp_lite.docconvert`` keeps working with it absent.

Resource layout notes:

* datas come from the source tree, not from the wheel: pyproject's
  package-data whitelist misses ``static/*.ico``, ``static/*.svg`` and
  ``static/*.js``; the exe takes the whole ``static/`` directory on purpose
  (superset of the whitelist).
* The ``kb_mcp_lite/`` prefix in the bundle is required by both lookup
  mechanisms: ``migrations/`` and ``templates|static/`` resolve through
  ``Path(__file__)`` and ``help/`` through
  ``importlib.resources.files("kb_mcp_lite")``.
* ``copy_metadata("mcp")`` is mandatory: ``mcp.server.fastmcp`` calls
  ``version("mcp")`` at import time with no fallback, so without the
  metadata every ``serve`` dies with PackageNotFoundError (kb itself
  hardcodes ``__version__`` and needs no metadata).
* ``collect_submodules("uvicorn")`` covers uvicorn's runtime
  ``import_from_string`` targets (protocols, loops, lifespan).
* ``collect_dynamic_libs("sqlite_vec")`` keeps ``vec0.dll`` at its
  ``sqlite_vec/vec0.dll`` relative path: sqlite_vec loads it relative to its
  own directory and a miss degrades silently (debug log only).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata

SPEC_DIR = Path(SPECPATH)
SRC = SPEC_DIR.parent / "src" / "kb_mcp_lite"

datas = [
    (str(SRC / "migrations"), "kb_mcp_lite/migrations"),
    (str(SRC / "help"), "kb_mcp_lite/help"),
    (str(SRC / "templates" / "admin"), "kb_mcp_lite/templates/admin"),
    (str(SRC / "static"), "kb_mcp_lite/static"),
]
datas += copy_metadata("mcp")

hiddenimports = collect_submodules("uvicorn") + collect_submodules("python_multipart")

# doc_ingestion and its transitive closure are out of scope for this exe; the
# clean exe-venv already lacks them, these excludes are the second lock.
excludes = [
    "doc_ingestion",
    "tiktoken",
    "pymupdf",
    "fitz",
    "docx",
    "pptx",
    "openpyxl",
    "xlrd",
    "langchain_core",
    "langchain_text_splitters",
    "langsmith",
]

binaries = collect_dynamic_libs("sqlite_vec")

a = Analysis(  # noqa: F821  (Analysis/PYZ/EXE are injected by PyInstaller)
    [str(SPEC_DIR / "kb_exe_entry.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="kb-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)