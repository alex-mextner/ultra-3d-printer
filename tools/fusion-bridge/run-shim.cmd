@echo off
REM Launches the Fusion MCP stdio shim using Fusion 360's own bundled CPython.
REM The interpreter lives under a per-release hash directory that changes on every
REM Fusion update, so it is resolved at run time rather than hardcoded.

setlocal

set "PROD=%LOCALAPPDATA%\Autodesk\webdeploy\production"
set "PYEXE="

for /f "delims=" %%P in ('dir /b /s /a-d "%PROD%\Python\python.exe" 2^>nul') do set "PYEXE=%%P"

if not defined PYEXE (
    echo Could not find Fusion 360's bundled python.exe under "%PROD%". 1>&2
    exit /b 1
)

"%PYEXE%" "%~dp0fusion_mcp_shim.py"
