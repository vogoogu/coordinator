@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if not errorlevel 1 (
  python pult.py
  goto end
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 pult.py
  goto end
)
echo Python not found. Install Python 3 from python.org and tick "Add to PATH".
:end
pause
