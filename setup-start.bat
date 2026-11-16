@echo off
cd /d "%~dp0"
python -m pip install --upgrade certifi --quiet 2>nul
pip install --prefer-binary -r backend\requirements.txt
python launcher.py
echo The script will exit after you continue.
pause