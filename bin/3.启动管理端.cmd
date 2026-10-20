@echo off
rem Start kb-mcp.exe in HTTP mode (streamable HTTP; what Cline connects to).
rem
rem   token     : read from %USERPROFILE%\.kb-mcp-http-token - never stored
rem               in this directory. Missing or empty file = refuse to start
rem               (the exe refuses a tokenless HTTP start anyway).
rem   data root : KB_MCP_HOME, default %USERPROFILE%\.local\share\kb-mcp
rem               (points back at the existing vaults; override to change)
rem   endpoint  : http://127.0.0.1:<KB_MCP_HTTP_PORT>/mcp (default port 8000)
rem
rem This file is ASCII-only ON PURPOSE: the console codepage on this machine
rem is 932, and non-ASCII bytes inside a .cmd desync CMD's line reader.
rem Keep it ASCII and keep the line endings CRLF.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title kb-mcp HTTP

if not exist "kb-mcp.exe" goto :no_exe
if not defined KB_MCP_HOME set "KB_MCP_HOME=%USERPROFILE%\.local\share\kb-mcp"
if not defined KB_MCP_HTTP_PORT set "KB_MCP_HTTP_PORT=8000"
set "TOKENFILE=%USERPROFILE%\.kb-mcp-http-token"
if not exist "%TOKENFILE%" goto :no_token
set "KB_MCP_HTTP_TOKEN="
set /p KB_MCP_HTTP_TOKEN=<"%TOKENFILE%"
if not defined KB_MCP_HTTP_TOKEN goto :no_token

echo starting kb-mcp (http) ...
echo   data root : %KB_MCP_HOME%
echo   endpoint  : http://127.0.0.1:8888/
echo   stop      : close this window (or Ctrl-C)
echo.
"%~dp0kb-mcp.exe" admin start
echo.
echo kb-mcp exited with code %ERRORLEVEL%.
echo.
start http://127.0.0.1:8888/app
pause
exit /b 0

:no_exe
echo [X] kb-mcp.exe is not next to this script - keep the launcher and the
echo     exe in the same directory.
pause
exit /b 1

:no_token
echo [X] no HTTP token: %TOKENFILE% is missing or empty.
echo     create it first (PowerShell):
echo       Set-Content -NoNewline '%TOKENFILE%' ([guid]::NewGuid().ToString('N'))
echo     then double-click this script again.
pause
exit /b 1