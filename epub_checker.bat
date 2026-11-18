@echo off
setlocal
cd /d %~dp0
set "PY=py -3"
where py >nul 2>nul || set "PY=python"
start "" %PY% -w epub_checker_app.py
endlocal
