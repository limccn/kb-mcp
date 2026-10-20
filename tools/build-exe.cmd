@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  kb-mcp-lite - build the kb-mcp.exe onefile bundle with
rem  PyInstaller and lay it into the top-level kbmcp-rt\ directory.
rem
rem  usage: build-exe.cmd
rem
rem  Steps, each one refusing to continue:
rem    1  uv build --wheel      -> dist\exe-build\kb_mcp_lite-<ver>-py3-none-any.whl
rem         (the out dir is wiped first: "the wheel" below must be the
rem         one just built, never a leftover from an earlier version)
rem    2  uv venv               -> dist\exe-venv (python 3.12)
rem    3  count the .py files in THAT wheel and refuse 0 (the empty-wheel
rem         trap measured on doc-ingestion: installs fine, imports nothing)
rem    4  uv pip install        -> that wheel with [vec,v0_8] + pyinstaller
rem    5  assert the venv is clean: no doc-ingestion / tiktoken / pymupdf
rem    6  PyInstaller --clean   -> dist\exe\kb-mcp.exe (log dist\exe-build.log)
rem    7  assert + smoke        -> exe exists, size recorded, --version rc=0
rem    8  copy                 -> ..\kbmcp-rt\kb-mcp.exe (idempotent)
rem
rem  The venv is separate from build-install.cmd's dist\venv on purpose:
rem  that one carries doc_ingestion + tiktoken, and this exe must not
rem  contain the converter tree (user decision 2026-10-09). Building from
rem  a clean environment removes the risk at the source. This script does
rem  not need build-install.cmd and does not touch its venv.
rem
rem  This file is ASCII-only ON PURPOSE. The console codepage on this
rem  machine is 932, and non-ASCII bytes inside a .cmd desync CMD's line
rem  reader - it starts executing rem text as commands. Keep it ASCII,
rem  and keep the line endings CRLF.
rem
rem  Failure exits sit outside parenthesised blocks (label jumps): a
rem  nested exit was measured to print its message and still return 0 in
rem  some shapes (see sql2orm\tools\build-db.cmd).
rem ============================================================

cd /d "%~dp0"
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
for %%I in ("%HERE%\..") do set "PKG=%%~fI"
for %%I in ("%PKG%\..") do set "REPO=%%~fI"
set "DIST=%PKG%\dist"
set "EXEBUILD=%DIST%\exe-build"
set "VENVDIR=%DIST%\exe-venv"
set "PYEXE=%VENVDIR%\Scripts\python.exe"
set "DISTEXE=%DIST%\exe"
set "LOG=%DIST%\exe-build.log"
set "RT=%REPO%\kbmcp-rt"
set "PYVER=3.12"
set "REQEXTRAS=[vec,v0_8]"

if not exist "%PKG%\pyproject.toml" goto :no_pyproject
if not exist "%HERE%\kb-mcp.spec" goto :no_spec
if not exist "%HERE%\kb_exe_entry.py" goto :no_entry
where uv >nul 2>nul
if errorlevel 1 goto :no_uv

rem --- step 1: the wheel, into a freshly wiped out dir ------------------
echo [1/8] building the kb-mcp-lite wheel...
if exist "%EXEBUILD%" rmdir /s /q "%EXEBUILD%"
mkdir "%EXEBUILD%" 2>nul
cd /d "%PKG%"
uv build --wheel --out-dir "%EXEBUILD%"
set "RC=%ERRORLEVEL%"
cd /d "%HERE%"
if not "%RC%"=="0" goto :build_failed

set "WHEEL="
set /a WHEELN=0
for %%F in ("%EXEBUILD%\*.whl") do (
  set "WHEEL=%%F"
  set /a WHEELN+=1
)
if not "%WHEELN%"=="1" goto :wheel_count
for %%F in ("%WHEEL%") do echo       %%~nxF

rem --- step 2: the venv --------------------------------------------------
set "STATE="
if not exist "%PYEXE%" (
  echo [2/8] creating %VENVDIR% with python %PYVER%...
  uv venv "%VENVDIR%" --python %PYVER%
  if errorlevel 1 set "STATE=venv"
) else (
  echo [2/8] venv present: %VENVDIR%
)
if "%STATE%"=="venv" goto :venv_failed

rem --- step 3: the wheel is not empty ------------------------------------
set "MARK=%TEMP%\kb-exe-wheel-%RANDOM%%RANDOM%.txt"
set "PYN="
"%PYEXE%" -c "import sys,zipfile; n=sum(1 for x in zipfile.ZipFile(sys.argv[1]).namelist() if x.endswith('.py')); print(n)" "%WHEEL%" > "%MARK%" 2>&1
if errorlevel 1 goto :count_failed
set /p PYN=<"%MARK%"
if not defined PYN goto :count_failed
if "%PYN%"=="0" goto :wheel_empty
echo       %PYN% .py files in the wheel
del "%MARK%" 2>nul

rem --- step 4: install THAT wheel + pyinstaller --------------------------
rem PEP 508 direct reference: the only form that keeps [extras] attached
rem to a wheel file (same reason as build-install.cmd step 4).
set "WHEELURL=%WHEEL:\=/%"
set "REQ=kb-mcp-lite%REQEXTRAS% @ file:///%WHEELURL%"
echo [4/8] installing %REQ% + pyinstaller
uv pip install --python "%PYEXE%" --reinstall "%REQ%" pyinstaller
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :install_failed

rem --- step 5: the venv must stay clean of the converter tree ------------
set "MARK=%TEMP%\kb-exe-list-%RANDOM%%RANDOM%.txt"
uv pip list --python "%PYEXE%" > "%MARK%" 2>&1
if errorlevel 1 goto :list_failed
findstr /i /c:"kb-mcp-lite" "%MARK%" >nul
if errorlevel 1 goto :list_no_kb
findstr /i /c:"pyinstaller " "%MARK%" >nul
if errorlevel 1 goto :list_no_pyi
findstr /i /c:"doc-ingestion" /c:"doc_ingestion" /c:"tiktoken" /c:"pymupdf" "%MARK%" >nul
if not errorlevel 1 goto :venv_dirty
echo [5/8] exe-venv is clean: no doc-ingestion / tiktoken / pymupdf
del "%MARK%" 2>nul

rem --- step 6: PyInstaller -------------------------------------------------
echo [6/8] building the onefile exe with PyInstaller (takes minutes)...
"%PYEXE%" -m PyInstaller --clean -y --workpath "%DIST%\exe-work" --distpath "%DISTEXE%" "%HERE%\kb-mcp.spec" > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :pyi_failed
echo       build log: %LOG%

rem Every warning naming a missing module has to be handled or recorded
rem before this build counts as done; list those lines here.
findstr /i /c:"missing" /c:"warning" "%LOG%"

rem --- step 7: the exe itself, and one real run of it ----------------------
set "KBEXE=%DISTEXE%\kb-mcp.exe"
if not exist "%KBEXE%" goto :no_exe
for %%F in ("%KBEXE%") do echo [7/8] %%~nxF: %%~zF bytes (in %DISTEXE%)
set "SMOKEHOME=%TEMP%\kb-exe-smoke-%RANDOM%%RANDOM%"
mkdir "%SMOKEHOME%" 2>nul
set "KB_MCP_HOME=%SMOKEHOME%"
set "MARK=%TEMP%\kb-exe-ver-%RANDOM%%RANDOM%.txt"
"%KBEXE%" --version > "%MARK%" 2>&1
if errorlevel 1 goto :version_failed
findstr /c:"kb, version 0.8.7" "%MARK%" >nul
if errorlevel 1 goto :version_wrong
for /f "usebackq delims=" %%V in ("%MARK%") do echo       %%V
del "%MARK%" 2>nul
rmdir /s /q "%SMOKEHOME%" 2>nul

rem --- step 8: lay it into kbmcp-rt\ ---------------------------------------
echo [8/8] copying into %RT%
if not exist "%RT%" mkdir "%RT%" 2>nul
copy /y "%KBEXE%" "%RT%\kb-mcp.exe" >nul
if errorlevel 1 goto :copy_failed
for %%F in ("%RT%\kb-mcp.exe") do echo       %%~nxF: %%~zF bytes

echo.
echo ============================================================
echo  [OK] kb-mcp.exe built and verified
echo.
echo   exe     : "%RT%\kb-mcp.exe"
echo   version : kb, version 0.8.7
echo   venv    : %VENVDIR%
echo   log     : %LOG%
echo ============================================================
exit /b 0

rem --- failure paths (outside any parenthesised block) -------------------

:no_pyproject
echo.
echo [X] no pyproject.toml in %PKG%
echo     this script expects to live in ^<package^>\tools\.
echo.
pause
exit /b 1

:no_spec
echo.
echo [X] no kb-mcp.spec next to this script (%HERE%)
echo.
pause
exit /b 1

:no_entry
echo.
echo [X] no kb_exe_entry.py next to this script (%HERE%)
echo.
pause
exit /b 1

:no_uv
echo.
echo [X] uv is not on PATH - install it first (this script builds with uv).
echo.
pause
exit /b 1

:build_failed
echo.
echo [X] uv build failed, exit code %RC%
echo.
pause
exit /b 1

:wheel_count
echo.
echo [X] expected exactly one .whl in %EXEBUILD%, found %WHEELN%
echo     the dir is wiped before the build; zero means the build produced
echo     nothing, more than one would make the pick ambiguous.
echo.
pause
exit /b 1

:venv_failed
echo.
echo [X] uv venv failed. If python %PYVER% is not on this machine, uv can
echo     fetch one; check the message above for what it could not find.
echo.
pause
exit /b 1

:count_failed
echo.
echo [X] could not count the .py files in the wheel:
type "%MARK%"
echo.
pause
exit /b 1

:wheel_empty
echo.
echo [X] the wheel contains no .py files - an empty wheel that would
echo     install fine and import nothing. check package discovery in
echo     %PKG%\pyproject.toml
echo.
pause
exit /b 1

:install_failed
echo.
echo [X] uv pip install failed, exit code %RC%
echo.
pause
exit /b 1

:list_failed
echo.
echo [X] uv pip list failed:
type "%MARK%"
echo.
pause
exit /b 1

:list_no_kb
echo.
echo [X] kb-mcp-lite is not in %VENVDIR% after the install:
type "%MARK%"
echo.
pause
exit /b 1

:list_no_pyi
echo.
echo [X] pyinstaller is not in %VENVDIR% after the install:
type "%MARK%"
echo.
pause
exit /b 1

:venv_dirty
echo.
echo [X] %VENVDIR% contains doc-ingestion / tiktoken / pymupdf:
findstr /i /c:"doc-ingestion" /c:"doc_ingestion" /c:"tiktoken" /c:"pymupdf" "%MARK%"
echo     this exe must not carry the converter tree; delete the venv
echo     (%VENVDIR%) and rebuild it from scratch.
echo.
pause
exit /b 1

:pyi_failed
echo.
echo [X] PyInstaller failed, exit code %RC%
echo     full log: %LOG%
echo.
pause
exit /b 1

:no_exe
echo.
echo [X] PyInstaller exited 0 but %KBEXE% does not exist
echo     full log: %LOG%
echo.
pause
exit /b 1

:version_failed
echo.
echo [X] kb-mcp.exe --version failed:
type "%MARK%"
echo.
pause
exit /b 1

:version_wrong
echo.
echo [X] kb-mcp.exe --version printed an unexpected version (want 0.8.7):
type "%MARK%"
echo.
pause
exit /b 1

:copy_failed
echo.
echo [X] copy of the exe into %RT% failed
echo.
pause
exit /b 1