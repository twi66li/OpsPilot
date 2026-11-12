@echo off
cd /d "%~dp0.."
if "%~1"=="" (echo Please drag a PDF onto this file. & pause & exit /b 1)
".venv\Scripts\python.exe" -m scripts.parse_pdf "%~1"
pause
