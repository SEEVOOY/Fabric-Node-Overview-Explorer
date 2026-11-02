@echo off
rem Starts Fabric Explorer with the workspace in fabric_explorer\config.py (or pick one in the app).
cd /d "%~dp0"
python run.py %*
if errorlevel 1 pause
