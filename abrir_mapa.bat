@echo off
cd /d "%~dp0"
python -m streamlit run app_mapa.py --server.port 8502 --server.address localhost
pause
