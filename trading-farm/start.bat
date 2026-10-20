@echo off
REM Aktien Data Farm - Doppelklick unter Windows
cd /d "%~dp0"
set PY=python
where py >nul 2>nul
if %errorlevel%==0 set PY=py
if not exist .venv (
  echo Erster Start: richte Python-Umgebung ein ...
  %PY% -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
python farm.py all
pause
