@echo off
REM HunterJobs ATS — Windows quick start
REM Double-click this file to launch the dashboard.

cd /d "%~dp0"

REM Check if keys.py exists
if not exist "keys.py" (
    echo.
    echo  [!] keys.py not found.
    echo      Copy keys_dummy.py to keys.py and add your API key first.
    echo      Get a free Google API key at https://aistudio.google.com/apikey
    echo.
    pause
    exit /b 1
)

echo Starting HunterJobs ATS...
echo Open http://localhost:8080 in your browser.
echo Press Ctrl+C to stop.
echo.

python dashboard.py

REM Pause on exit so you can read any errors
if errorlevel 1 (
    echo.
    echo Dashboard exited with error code %errorlevel%.
    pause
)
