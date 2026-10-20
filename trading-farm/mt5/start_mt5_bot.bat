@echo off
title Aktien Data Farm - MT5-Autopilot
color 0A
REM Doppelklick unter Windows startet den Autopiloten (nur Demokonto)
cd /d "%~dp0"
set PY=python
where py >nul 2>nul
if %errorlevel%==0 set PY=py
%PY% --version >nul 2>&1
if errorlevel 1 (
  echo FEHLER: Python nicht gefunden! Python 3.12 installieren, Haken bei "Add Python to PATH".
  pause
  exit /b 1
)
if not exist ..\.venv (
  echo Erster Start: richte Python-Umgebung ein ...
  %PY% -m venv ..\.venv
)
call ..\.venv\Scripts\activate.bat
pip install -q "pandas>=2.0" "numpy>=1.24" MetaTrader5
if errorlevel 1 (
  echo FEHLER bei der Installation. Siehe ANLEITUNG_MT5.md - Abschnitt Probleme.
  pause
  exit /b 1
)
echo.
echo Denk dran: MetaTrader 5 muss laufen, mit dem DEMOKONTO eingeloggt sein
echo und der Knopf "Algo Trading" muss gruen sein!
echo Im Fenster muss gleich stehen: "Handel ueber die MT5-Python-API ist erlaubt."
echo Stoppen: Strg + C
echo ------------------------------------------------------------
python bot_smc.py
echo ------------------------------------------------------------
echo Bot wurde beendet.
pause
