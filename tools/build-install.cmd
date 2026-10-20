@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  kb-mcp-lite - build the wheels from source, install the CLI, the
rem  MCP server and the doc-ingestion converters, then prove they work.
rem
rem  usage: build-install.cmd [--venv <dir>] [--python <spec>] [--no-extras]
rem
rem    --venv   <dir>   where to install; default <package>\dist\venv,
rem                     created if absent. A fresh venv is deliberate: the
rem                     project's own .venv holds an *editable* install that
rem                     every `uv run` / `uv sync` puts back, so installing
rem                     there would look like it worked and quietly be
rem                     replaced the next time anyone ran uv.
rem    --python <spec>  interpreter for that venv, default 3.12. Pinned on
rem                     purpose: the vec0 extra leans on pysqlite3, which
rem                     ships wheels for 3.10..3.13, and a newer interpreter
rem                     turns the extra into a silent no-op. doc-ingestion
rem                     separately requires 3.12 or newer, so anything below
rem                     3.12 fails at its install step instead.
rem    --no-extras      install the bare wheel: no pysqlite3 / sqlite-vec
rem                     (semantic search degrades to disabled) and no
rem                     watchfiles (the watcher falls back to polling).
rem
rem  Steps, each one refusing to continue:
rem    1  uv build --wheel            -> dist\kb_mcp_lite-<version>-py3-none-any.whl
rem    2  uv venv                     -> the venv, only if it is missing
rem    3  uv build --wheel            -> dist\doc-ingestion\doc_ingestion-<version>-py3-none-any.whl
rem         then count the .py files inside that wheel and refuse an empty
rem         one: hatchling's package-path trap builds a wheel that installs
rem         cleanly and imports nothing.
rem    4  uv pip install --reinstall  -> the kb-mcp-lite wheel, with [vec,v0_8]
rem    5  uv pip install --reinstall  -> the doc-ingestion wheel
rem    6  smoke test                  -> see below
rem
rem  doc-ingestion is built from ..\ingestion and installed into the same
rem  venv on purpose: it is what tools\import-docs.cmd converts documents
rem  with, and the conversion path exists only if that wheel is in this
rem  venv. The two projects are built together because they are installed
rem  together.
rem
rem  The smoke test is the point of the exercise. A wheel can be built
rem  without its runtime assets (migrations\*.sql, help, templates, static)
rem  and still install cleanly; it then fails on the first document write.
rem  So this counts the migrations inside site-packages and then, in a
rem  THROWAWAY KB_MCP_HOME, writes a document and reads it back - five
rem  claims, each of which can fail: --version, add, doctor, search, and an
rem  import of both installed packages from the venv. The real vault under
rem  %USERPROFILE%\.local\share\kb-mcp is never touched (the same isolation
rem  kbmcp\e2e\run.py uses).
rem
rem  Exit code: 0 = all five smoke checks passed, 1 = anything failed.
rem
rem  This file is ASCII-only ON PURPOSE. The console codepage on this
rem  machine is 932, and non-ASCII bytes inside a .cmd desync CMD's line
rem  reader - it starts executing rem text as commands. Keep it ASCII, and
rem  keep the line endings CRLF.
rem
rem  Failure exits sit outside parenthesised blocks that have commands
rem  after them: an `exit /b 1` nested inside one prints its message and
rem  still returns 0. sql2orm\tools\build-db.cmd carries the measurement.
rem ============================================================

cd /d "%~dp0"

rem Resolved through %%~fI rather than kept as "<dir>\..": a path printed as
rem dist\venv\..\.. is one a reader has to evaluate before they can tell
rem whether it is right, and this script prints what it resolved.
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
for %%I in ("%HERE%\..") do set "PKG=%%~fI"
set "DIST=%PKG%\dist"
set "VENVDIR=%DIST%\venv"
set "PYVER=3.12"
set "EXTRAS=[vec,v0_8]"
for %%I in ("%PKG%\..\ingestion") do set "ING=%%~fI"
set "INGDIST=%DIST%\doc-ingestion"

rem --- arguments -------------------------------------------------------
rem Label-parsed, not `if (...) ( shift )`: a parenthesised block is expanded
rem once, before shift runs, so %~1 inside it would still be the old first.
:arg_loop
if "%~1"=="" goto :arg_done
if /i "%~1"=="--venv" goto :arg_venv
if /i "%~1"=="--python" goto :arg_python
if /i "%~1"=="--no-extras" goto :arg_noextras
if /i "%~1"=="--help" goto :arg_help
if /i "%~1"=="-h" goto :arg_help
echo [X] unknown option: %~1
goto :arg_usage

:arg_venv
set "VENVDIR=%~2"
if not defined VENVDIR goto :arg_missing
shift
shift
goto :arg_loop

:arg_python
set "PYVER=%~2"
if not defined PYVER goto :arg_missing
shift
shift
goto :arg_loop

:arg_noextras
set "EXTRAS="
shift
goto :arg_loop

:arg_missing
echo [X] that option needs a value.
goto :arg_usage

:arg_help
echo  usage: build-install.cmd [--venv ^<dir^>] [--python ^<spec^>] [--no-extras]
echo.
echo    --venv   ^<dir^>   install into this environment
echo                      default: ^<package^>\dist\venv
echo    --python ^<spec^>  interpreter for a new venv, default 3.12
echo    --no-extras       skip the vec0 / watchfiles extras
echo.
echo  Builds dist\kb_mcp_lite-^<version^>-py3-none-any.whl and
echo  dist\doc-ingestion\doc_ingestion-^<version^>-py3-none-any.whl, installs
echo  both, then smoke-tests them in a throwaway KB_MCP_HOME.
echo.
pause
exit /b 0

:arg_usage
echo.
echo  usage: build-install.cmd [--venv ^<dir^>] [--python ^<spec^>] [--no-extras]
echo.
pause
exit /b 1

:arg_done

echo.
echo ============================================================
echo  kb-mcp-lite  build + install
echo ============================================================
echo   package : %PKG%
echo   venv    : %VENVDIR%
echo   extras  : %EXTRAS%
echo.

rem --- check 1: uv present (it builds, creates the venv and installs) ---
where uv >nul 2>nul
if errorlevel 1 (
  echo [X] uv not found on PATH. Install it: https://docs.astral.sh/uv/
  echo.
  pause
  exit /b 1
)

rem --- check 2: this is the package root ---
if not exist "%PKG%\pyproject.toml" (
  echo [X] no pyproject.toml in %PKG%
  echo     tools\build-install.cmd expects to live in ^<package^>\tools\.
  echo.
  pause
  exit /b 1
)

rem --- check 3: the sibling converter project ---
if not exist "%ING%\pyproject.toml" (
  echo [X] no pyproject.toml in %ING%
  echo     the doc-ingestion wheel is built from ..\ingestion; this script
  echo     expects the two projects to sit side by side.
  echo.
  pause
  exit /b 1
)

rem --- step 1: build the wheel -----------------------------------------
rem Built from the package root, not from tools\: uv takes the project to
rem build from the current directory, and tools\ is not a Python project.
echo [1/6] building the kb-mcp-lite wheel...
cd /d "%PKG%"
uv build --wheel --out-dir "%DIST%"
set "RC=%ERRORLEVEL%"
cd /d "%HERE%"
if not "%RC%"=="0" (
  echo.
  echo [X] uv build failed, exit code %RC%
  echo.
  pause
  exit /b 1
)

rem Newest first: a leftover wheel from an older version must not be
rem picked up just because it sorts earlier.
set "WHEEL="
for /f "delims=" %%F in ('dir /b /o-d "%DIST%\*.whl" 2^>nul') do if not defined WHEEL set "WHEEL=%DIST%\%%F"
if not defined WHEEL (
  echo [X] uv build reported success but no .whl is in %DIST%
  echo.
  pause
  exit /b 1
)
for %%F in ("%WHEEL%") do echo       %%~nxF

rem --- step 2: the venv ------------------------------------------------
rem Created only when missing: a re-run installs the new wheel into the
rem environment that already exists, which is what "rebuild and reinstall"
rem means. Delete the directory to get a clean one.
set "FRESH="
set "STATE="
if not exist "%VENVDIR%\Scripts\python.exe" set "FRESH=1"
if defined FRESH (
  echo [2/6] creating %VENVDIR% with python %PYVER%...
  uv venv "%VENVDIR%" --python %PYVER%
  if errorlevel 1 set "STATE=venv"
)
if "%STATE%"=="venv" (
  echo.
  echo [X] uv venv failed. If python %PYVER% is not on this machine, uv can
  echo     fetch one; check the message above for what it could not find.
  echo.
  pause
  exit /b 1
)
if not defined FRESH echo [2/6] venv present: %VENVDIR%

rem --- step 3: the doc-ingestion wheel ----------------------------------
rem Built from the sibling project, into its own subdirectory of dist\: the
rem kb-mcp-lite wheel is found by taking the newest *.whl in dist, and two
rem wheel families in one directory would make that pick ambiguous.
set "PYEXE=%VENVDIR%\Scripts\python.exe"
set "STATE="
echo [3/6] building the doc-ingestion wheel...
if not exist "%INGDIST%" mkdir "%INGDIST%" 2>nul
uv build --wheel --out-dir "%INGDIST%" "%ING%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [X] uv build failed for %ING%, exit code %RC%
  echo.
  pause
  exit /b 1
)
set "INGWHEEL="
for /f "delims=" %%F in ('dir /b /o-d "%INGDIST%\*.whl" 2^>nul') do if not defined INGWHEEL set "INGWHEEL=%INGDIST%\%%F"
if not defined INGWHEEL (
  echo [X] uv build reported success but no .whl is in %INGDIST%
  echo.
  pause
  exit /b 1
)

rem The count is the difference between a wheel and an empty one. Measured
rem trap: hatchling with a wrong package path builds a tiny wheel, exits 0
rem and prints "Successfully built"; it then installs cleanly and imports
rem nothing. Counting .py entries is the assertion that can fail.
set "INGMARK=%TEMP%\doc-ingestion-wheel-%RANDOM%%RANDOM%.txt"
set "PYN="
"%PYEXE%" -c "import sys,zipfile; n=sum(1 for x in zipfile.ZipFile(sys.argv[1]).namelist() if x.endswith('.py')); print(n)" "%INGWHEEL%" > "%INGMARK%" 2>&1
if errorlevel 1 set "STATE=ingcount"
set /p PYN=<"%INGMARK%"
if not defined PYN set "STATE=ingcount"
if "%STATE%"=="ingcount" (
  echo.
  echo [X] could not count the .py files in the doc-ingestion wheel:
  type "%INGMARK%"
  echo.
  pause
  exit /b 1
)
if "%PYN%"=="0" set "STATE=ingempty"
if "%STATE%"=="ingempty" (
  echo.
  echo [X] the doc-ingestion wheel contains no .py files - an empty wheel
  echo     that would install fine and import nothing. check the
  echo     [tool.hatch.build.targets.wheel] packages path in %ING%\pyproject.toml
  echo.
  pause
  exit /b 1
)
for %%F in ("%INGWHEEL%") do echo       %%~nxF - %PYN% .py files
del "%INGMARK%" 2>nul

rem --- step 4: install the wheel into it --------------------------------
rem --reinstall, not a version check: the point is to install THIS wheel,
rem and the environment may already carry the same version from elsewhere.
rem The requirement is written as a PEP 508 direct reference because that is
rem the only form that keeps the [extras] attached to a wheel file.
set "WHEELURL=%WHEEL:\=/%"
set "REQ=kb-mcp-lite%EXTRAS% @ file:///%WHEELURL%"
echo [4/6] installing %REQ%
uv pip install --python "%VENVDIR%\Scripts\python.exe" --reinstall "%REQ%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [X] uv pip install failed, exit code %RC%
  echo.
  pause
  exit /b 1
)

rem --- step 5: the doc-ingestion wheel, into the same venv --------------
set "INGWHEELURL=%INGWHEEL:\=/%"
set "INGREQ=doc-ingestion @ file:///%INGWHEELURL%"
echo [5/6] installing %INGREQ%
uv pip install --python "%VENVDIR%\Scripts\python.exe" --reinstall "%INGREQ%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [X] uv pip install failed for doc-ingestion, exit code %RC%
  echo.
  pause
  exit /b 1
)

set "KBEXE=%VENVDIR%\Scripts\kb.exe"
set "SITE=%VENVDIR%\Lib\site-packages"
set "STATE="

rem --- step 4a: the entry point exists and answers ----------------------
if not exist "%KBEXE%" set "STATE=entrypoint"
if "%STATE%"=="entrypoint" (
  echo.
  echo [X] no kb.exe in %VENVDIR%\Scripts - the console script did not install.
  echo.
  pause
  exit /b 1
)

rem --- step 4b: the wheel carried its runtime assets --------------------
rem A wheel built without package-data installs perfectly and then dies on
rem the first write, because the schema migrations are not there. Counting
rem them is the difference between "installed" and "usable".
set "MIGRATIONS=0"
for %%F in ("%SITE%\kb_mcp_lite\migrations\*.sql") do set /a MIGRATIONS+=1
if "%MIGRATIONS%"=="0" set "STATE=assets"
if "%STATE%"=="assets" (
  echo.
  echo [X] no migrations in %SITE%\kb_mcp_lite\migrations
  echo     the wheel is missing its package data; every write would fail.
  echo.
  pause
  exit /b 1
)

rem An editable install would shadow the wheel with the source tree, which
rem is what this script exists to get away from. Only possible when --venv
rem points at a working copy's venv.
if exist "%SITE%\__editable__*" echo       [!] an editable install is also present in this venv

rem --- step 4c: smoke test in a throwaway KB_MCP_HOME -------------------
rem KB_MCP_HOME is redirected inside this setlocal, so it dies with the
rem script and the real vault is never opened. Each check below can fail:
rem what must not happen is a failure that still exits 0.
set "SMOKE=%TEMP%\kb-wheel-smoke-%RANDOM%%RANDOM%"
mkdir "%SMOKE%" 2>nul
set "KB_MCP_HOME=%SMOKE%"
set "MARK=%SMOKE%\marker-%RANDOM%.txt"
set "STATE="

echo [6/6] smoke test (KB_MCP_HOME=%SMOKE%)
"%KBEXE%" --version > "%MARK%" 2>&1
if errorlevel 1 set "STATE=version"
if "%STATE%"=="version" (
  echo [X] kb --version failed:
  type "%MARK%"
  echo.
  pause
  exit /b 1
)
for /f "usebackq delims=" %%V in ("%MARK%") do echo       %%V

"%KBEXE%" add --type spec --title "wheel smoke test" --body "marker text for the wheel smoke test" > "%MARK%" 2>&1
if errorlevel 1 set "STATE=add"
if "%STATE%"=="add" (
  echo [X] kb add failed:
  type "%MARK%"
  echo.
  pause
  exit /b 1
)
for /f "usebackq delims=" %%V in ("%MARK%") do echo       %%V

rem "kb doctor: OK" is the plain-text verdict. The --json form is not used
rem here on purpose: it prints one "ok": true per nested check, so a
rem findstr for that pattern passes even when the overall verdict is FAIL.
"%KBEXE%" doctor > "%MARK%" 2>&1
findstr /c:"kb doctor: OK" "%MARK%" >nul
if errorlevel 1 set "STATE=doctor"
if "%STATE%"=="doctor" (
  echo [X] kb doctor did not report OK:
  type "%MARK%"
  echo.
  pause
  exit /b 1
)

rem The round trip: the document written above must come back out of the
rem search index (id wheel-smoke-test, derived from type + title).
"%KBEXE%" search "marker" > "%MARK%" 2>&1
findstr /c:"wheel-smoke-test" "%MARK%" >nul
if errorlevel 1 set "STATE=search"
if "%STATE%"=="search" (
  echo [X] the document written a moment ago is not retrievable:
  type "%MARK%"
  echo.
  pause
  exit /b 1
)

rem --- step 4d: both packages import from this venv ---------------------
rem The kb CLI above proves kb_mcp_lite loads; this proves the converter
rem wheel does too, from the same interpreter import-docs.cmd will use.
rem -I: an isolated sys.path, so a stray json.py next to the cwd cannot
rem shadow a stdlib module and turn a real failure into a pass or back.
set "STATE="
"%PYEXE%" -I -c "import kb_mcp_lite, doc_ingestion; print('kb_mcp_lite + doc_ingestion import OK')" > "%MARK%" 2>&1
if errorlevel 1 set "STATE=import"
if "%STATE%"=="import" (
  echo [X] the installed packages do not import:
  type "%MARK%"
  echo.
  pause
  exit /b 1
)
for /f "usebackq delims=" %%V in ("%MARK%") do echo       %%V

rem The throwaway home goes away with the smoke test; KB_MCP_HOME dies with
rem the enclosing setlocal.
rmdir /s /q "%SMOKE%" 2>nul

echo.
echo ============================================================
echo  [OK] wheel built, installed and verified
echo.
echo   wheel      : %WHEEL%
echo   venv       : %VENVDIR%
echo   CLI        : "%KBEXE%"
echo   MCP server : "%KBEXE%" serve --transport http
echo   migrations : %MIGRATIONS%
echo   converter  : "%INGWHEEL%"
echo.
echo  Next:
echo    import a folder : tools\import-docs.cmd --dir ^<folder^> --vault ^<vault^>
echo    the http transport refuses to start without KB_MCP_HTTP_TOKEN, and
echo    listens on http://127.0.0.1:8000/mcp unless KB_MCP_HTTP_PORT says
echo    otherwise.
echo ============================================================
echo.

pause
exit /b 0
