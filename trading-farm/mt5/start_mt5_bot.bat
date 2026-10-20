@echo off
title Aktien Data Farm - MT5-Autopilot
color 0A
REM Doppelklick unter Windows startet den Autopiloten (nur Demokonto)
cd /d "%~dp0"
REM Python-Version, fuer die es das MetaTrader5-Paket sicher gibt - sonst die Standard-Version
set PY=
for %%v in (3.12 3.11 3.13) do (
  if not defined PY py -%%v --version >nul 2>&1 && set "PY=py -%%v"
)
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY set "PY=python"
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
  echo FEHLER bei der Installation. Python 3.12 installieren, den Ordner .venv neben
  echo dem Ordner mt5 loeschen und diese Datei erneut starten. Siehe ANLEITUNG_MT5.md - Probleme.
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
