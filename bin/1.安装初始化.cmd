@echo off
rem Smoke test for kb-mcp.exe. Run it from this directory (double-click ok).
rem
rem   1  kb-mcp.exe --version prints "kb, version 0.8.7"
rem   2  a three-line stdio handshake (initialize + notifications/initialized
rem      + tools/list) is answered; stdout carries the protocol replies
rem
rem KB_MCP_HOME points at a throwaway directory, so the real data root is
rem never opened (same isolation as kbmcp\tools\build-install.cmd carries).
rem
rem This file is ASCII-only ON PURPOSE: the console codepage on this machine
rem is 932, and non-ASCII bytes inside a .cmd desync CMD's line reader.
rem Keep it ASCII and keep the line endings CRLF.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "kb-mcp.exe" goto :no_exe

set "HOME_TMP=%TEMP%\kb-mcp-smoke-%RANDOM%%RANDOM%"
set "REQ=%TEMP%\kb-mcp-smoke-req-%RANDOM%%RANDOM%.txt"
set "OUT=%TEMP%\kb-mcp-smoke-out-%RANDOM%%RANDOM%.txt"
set "ERR=%TEMP%\kb-mcp-smoke-err-%RANDOM%%RANDOM%.txt"
mkdir "%HOME_TMP%" 2>nul

echo [1/2] kb-mcp.exe --version
"%~dp0kb-mcp.exe" --version > "%OUT%" 2>&1
if errorlevel 1 goto :version_failed
findstr /c:"kb, version 0.8.7" "%OUT%" >nul
if errorlevel 1 goto :version_failed
type "%OUT%"
echo.

echo [2/2] stdio handshake: initialize + notifications/initialized + tools/list
> "%REQ%" echo {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}
>> "%REQ%" echo {"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
>> "%REQ%" echo {"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}
set "KB_MCP_HOME=%HOME_TMP%"
"%~dp0kb-mcp.exe" serve < "%REQ%" > "%OUT%" 2>"%ERR%"
if errorlevel 1 goto :handshake_failed
findstr /c:"serverInfo" "%OUT%" >nul
if errorlevel 1 goto :handshake_failed
findstr /c:"inputSchema" "%OUT%" >nul
if errorlevel 1 goto :handshake_failed
findstr /c:"kb_search" "%OUT%" >nul
if errorlevel 1 goto :handshake_failed
echo       initialize answered; tools/list answered; kb_search is present
echo.

del "%REQ%" 2>nul
del "%OUT%" 2>nul
del "%ERR%" 2>nul
rmdir /s /q "%HOME_TMP%" 2>nul
echo ============================================================
echo  [OK] kb-mcp.exe smoke test passed
echo ============================================================
echo.
pause
exit /b 0

:no_exe
echo [X] kb-mcp.exe is not next to this script - keep the launcher and the
echo     exe in the same directory.
pause
exit /b 1

:version_failed
echo [X] kb-mcp.exe --version failed or printed an unexpected version:
type "%OUT%"
echo.
pause
exit /b 1

:handshake_failed
echo [X] the stdio handshake did not answer as expected; stdout was:
type "%OUT%"
echo.
echo     stderr was:
type "%ERR%"
echo.
pause
exit /b 1