@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
python server.py --host 0.0.0.0 --port 8765
pause
