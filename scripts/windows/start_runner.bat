@echo off
cd /d %USERPROFILE%\polymarket_copybot
call .venv\Scripts\activate.bat
python copybot.py run-paper
