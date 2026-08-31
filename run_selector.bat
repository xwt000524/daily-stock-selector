@echo off
chcp 65001 >nul
python "%~dp0local_selector.py" %*
pause
