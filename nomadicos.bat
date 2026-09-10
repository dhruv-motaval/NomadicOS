@echo off
rem NomadicOS interactive CLI (ADR-0015)
cd /d "%~dp0"
set PYTHONPATH=%~dp0src
python -m nomadicos %*
