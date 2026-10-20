"""PyInstaller entry point for the ``kb-mcp`` onefile exe.

PyInstaller needs a script file as the analysis root; the package module
(``kb_mcp_lite.cli``) cannot be used directly. This file is part of the
build, not of the shipped artifacts (see ``build-exe.cmd`` /
``kb-mcp.spec`` next to it).

``prog_name="kb"`` is pinned on purpose: click derives the program name
from ``sys.executable`` in a frozen app, so without it the exe would print
``kb-mcp.exe, version ...`` and help texts would name the file instead of
the command. The exe is shipped as an installed ``kb`` CLI, so it presents
itself as one — same output as the pip-installed ``kb.exe``.
"""

from kb_mcp_lite.cli import cli

if __name__ == "__main__":
    cli(prog_name="kb")