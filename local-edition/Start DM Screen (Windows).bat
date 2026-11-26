@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py server.py & goto :end)
where python >nul 2>nul && (python server.py & goto :end)
echo Python is not installed. Get it free from https://www.python.org/downloads/ (tick "Add Python to PATH"), then try again.
:end
pause
