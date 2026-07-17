@echo off
chcp 65001 >nul
echo ========================================
echo   CPUSTACK 主计算节点启动
echo ========================================
cd /d "%~dp0\backend"
python -m cpustack.cli both --host 0.0.0.0 --port 8081
pause
