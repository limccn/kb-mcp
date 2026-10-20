@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  kb-mcp-lite - ingest a folder into one knowledge base (vault)
rem
rem  usage: import-docs.cmd --dir <folder> --vault <name>
rem                         [--type <doctype>] [--venv <dir>]
rem
rem    --dir   <folder>  folder to scan, recursively
rem    --vault <name>    target knowledge base; created if it does not
rem                      exist. Nothing else about the current vault is
rem                      touched - every call names its vault explicitly.
rem    --type  <doctype> type for documents this script generates from
rem                      converted files (default: spec). Copied .md files
rem                      keep whatever their own frontmatter says.
rem    --venv  <dir>     environment holding kb.exe
rem                      default: <package>\dist\venv (tools\build-install.cmd)
rem
rem  What it scans, and what happens to it:
rem    .md / .markdown  copied into a staging tree as-is
rem    .docx .pptx .xlsx .xlsm .xls .pdf .txt .json
rem                     converted by kb_mcp_lite.docconvert (the
rem                     doc_ingestion wheel in dist\venv, installed by
rem                     tools\build-install.cmd)
rem    .doc             antiword -m UTF-8.txt
rem    anything else    skipped and named - never dropped in silence
rem
rem  A folder named "converted" is reserved: that is where this script puts
rem  the documents it generates, so files of your own living there are
rem  skipped rather than merged into it.
rem
rem  The scan, the extraction and the import are done by tools\import_docs.py
rem  (the generalised form of testdoc-import\import.py); this file owns the
rem  arguments, the vault and the verification. The helper runs on this
rem  venv's own interpreter (%VENVDIR%\Scripts\python.exe), which holds
rem  both the doc_ingestion wheel and pyyaml - no uv, no throwaway
rem  environment.
rem
rem  Verification is the reason this is not a one-line wrapper. `kb import`
rem  exits 0 even when a file is rejected - the failures arrive only in the
rem  JSON report's "errors" list - so the helper reads that list and this
rem  script then counts the vault's documents before and after. A run that
rem  reports success with a count that did not move is visible here.
rem
rem  Japanese filenames: `kb import` derives an id from the file's path and
rem  the id validator (^[a-z0-9][a-z0-9/_-]*$) rejects a Japanese stem, so a
rem  copied .md whose name is Japanese needs an explicit `id:` in its
rem  frontmatter. Generated documents always get an ASCII id, which is why
rem  all converted types with Japanese names import without any such edit.
rem
rem  Exit code: 0 = every file landed, 1 = something failed (the reasons are
rem  printed, and the staging tree is kept for inspection), 2 = bad usage,
rem  a missing directory, or nothing ingestible in it.
rem
rem  This file is ASCII-only ON PURPOSE, with CRLF endings: the console
rem  codepage here is 932 and either one alone desyncs CMD's line reader.
rem  Failure exits sit outside parenthesised blocks that have commands after
rem  them - see sql2orm\tools\build-db.cmd for both measurements.
rem ============================================================

cd /d "%~dp0"

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
for %%I in ("%HERE%\..") do set "PKG=%%~fI"
set "VENVDIR=%PKG%\dist\venv"
set "TYPE=spec"
set "DIR="
set "VAULT="

rem --- arguments -------------------------------------------------------
rem Label-parsed, not `if (...) ( shift )`: a parenthesised block is expanded
rem once, before shift runs, so %~1 inside it would still be the old first.
:arg_loop
if "%~1"=="" goto :arg_done
if /i "%~1"=="--dir" goto :arg_dir
if /i "%~1"=="--vault" goto :arg_vault
if /i "%~1"=="--type" goto :arg_type
if /i "%~1"=="--venv" goto :arg_venv
if /i "%~1"=="--help" goto :arg_help
if /i "%~1"=="-h" goto :arg_help
echo [X] unknown option: %~1
goto :arg_usage

:arg_dir
set "DIR=%~2"
if not defined DIR goto :arg_missing
shift
shift
goto :arg_loop

:arg_vault
set "VAULT=%~2"
if not defined VAULT goto :arg_missing
shift
shift
goto :arg_loop

:arg_type
set "TYPE=%~2"
if not defined TYPE goto :arg_missing
shift
shift
goto :arg_loop

:arg_venv
set "VENVDIR=%~2"
if not defined VENVDIR goto :arg_missing
shift
shift
goto :arg_loop

:arg_missing
echo [X] that option needs a value.
goto :arg_usage

:arg_help
echo  usage: import-docs.cmd --dir ^<folder^> --vault ^<name^>
echo                         [--type ^<doctype^>] [--venv ^<dir^>]
echo.
echo    --dir   ^<folder^>  folder to scan, recursively
echo    --vault ^<name^>    target vault, created if missing
echo    --type  ^<doctype^> type for generated documents (default spec)
echo    --venv  ^<dir^>     environment holding kb.exe
echo                       default: ^<package^>\dist\venv
echo.
echo  Example:
echo    tools\import-docs.cmd --dir C:\docs\omron --vault omron
echo.
pause
exit /b 0

:arg_usage
echo.
echo  usage: import-docs.cmd --dir ^<folder^> --vault ^<name^>
echo                         [--type ^<doctype^>] [--venv ^<dir^>]
echo.
pause
exit /b 1

:arg_done
if not defined DIR goto :arg_required
if not defined VAULT goto :arg_required
goto :ready

:arg_required
echo [X] --dir and --vault are both required.
echo.
pause
exit /b 2

:ready
rem The direct kb calls below (vault create, stats) go through the same
rem child encoding as the helper's: cp932 second bytes can be 0x5C, which
rem is a backslash inside a path the moment any name is Japanese.
set "PYTHONIOENCODING=utf-8"
set "KBEXE=%VENVDIR%\Scripts\kb.exe"
set "HELPER=%HERE%\import_docs.py"
set "MARK=%TEMP%\kb-import-docs-%RANDOM%%RANDOM%.txt"
set "STATE="

echo.
echo ============================================================
echo  kb-mcp-lite  import folder
echo ============================================================
echo   folder : %DIR%
echo   vault  : %VAULT%
echo   type   : %TYPE%   (generated documents only)
echo   kb     : %KBEXE%
echo.

rem --- check 1: the folder is a real directory --------------------------
rem pushd is the test, not `if exist`: it answers "is this a directory I
rem can enter", which is the question, and it hands back the resolved
rem absolute path at the same time - the helper records document sources
rem relative to the folder it is given, so a relative --dir must not
rem survive to there.
pushd "%DIR%" >nul 2>nul
if errorlevel 1 (
  echo [X] not a directory: %DIR%
  echo.
  pause
  exit /b 2
)
for %%I in (.) do set "DIR=%%~fI"
popd

rem --- check 2: the installed tool, and the helper next to this file ----
if not exist "%KBEXE%" (
  echo [X] kb.exe not found: %KBEXE%
  echo     Run tools\build-install.cmd first, or point --venv at the
  echo     environment that already holds kb.
  echo.
  pause
  exit /b 2
)
if not exist "%HELPER%" (
  echo [X] import_docs.py not found: %HELPER%
  echo     it ships next to this script; the two must stay together.
  echo.
  pause
  exit /b 2
)

rem --- check 3: converters, but only as a warning -----------------------
rem A missing converter is not detected here by counting which file types
rem are present (that count belongs to the helper's own scan, one place,
rem one number). This says what is missing and lets the run show the
rem damage per file, which is the honest version of the same information.
where antiword >nul 2>nul
if errorlevel 1 echo       [!] antiword not on PATH - every .doc will fail

rem --- the vault must exist before anything names it --------------------
rem `kb --vault <name>` against an unknown name dies with a raw traceback
rem (VaultNotFoundError escapes the CLI's handler), so create first and
rem treat "already exists" as the success it is.
"%KBEXE%" vault create "%VAULT%" > "%MARK%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  findstr /i /c:"already exists" "%MARK%" >nul
  if errorlevel 1 set "STATE=vault"
)
if "%STATE%"=="vault" (
  echo [X] could not create or reach vault "%VAULT%":
  type "%MARK%"
  echo.
  pause
  exit /b 2
)
if "%RC%"=="0" echo   vault created.

rem --- documents before ------------------------------------------------
set "BEFORE="
"%KBEXE%" --vault "%VAULT%" stats > "%MARK%" 2>&1
for /f "usebackq tokens=3" %%N in (`findstr /b /c:"Total documents:" "%MARK%"`) do set "BEFORE=%%N"
if not defined BEFORE echo       [!] could not read the document count before the run
if defined BEFORE echo   documents before: %BEFORE%

rem --- the run ----------------------------------------------------------
echo.
"%VENVDIR%\Scripts\python.exe" "%HELPER%" --dir "%DIR%" --vault "%VAULT%" --kb "%KBEXE%" --type "%TYPE%"
set "RC=%ERRORLEVEL%"

rem --- documents after -------------------------------------------------
set "AFTER="
"%KBEXE%" --vault "%VAULT%" stats > "%MARK%" 2>&1
for /f "usebackq tokens=3" %%N in (`findstr /b /c:"Total documents:" "%MARK%"`) do set "AFTER=%%N"
del "%MARK%" 2>nul

echo.
echo ============================================================
if not "%RC%"=="0" (
  echo  [X] import finished with failures - exit code %RC%
  echo      the reasons are above, per file; a staging tree was kept
  echo      if any file failed inside the import itself.
) else (
  echo  [OK] import finished
)
if defined BEFORE if defined AFTER echo   documents in "%VAULT%": %BEFORE% -^> %AFTER%
rem Only meaningful on a clean run: when files failed, an unchanged count is
rem explained by the failure, and this hint would send the reader looking for
rem an idempotency problem that is not there.
if "%RC%"=="0" if defined BEFORE if defined AFTER if "%BEFORE%"=="%AFTER%" echo   [!] unchanged - re-importing the same files updates documents
echo.
echo   check with: "%KBEXE%" --vault "%VAULT%" stats
echo               "%KBEXE%" --vault "%VAULT%" search ^<word^>
echo ============================================================
echo.

pause
exit /b %RC%