@echo off
REM CPUSTACK 本地启动脚本（SQLite + Server + Worker 单机模式）
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   CPUSTACK 本地启动（SQLite + Server）
echo ============================================

REM 设置环境变量（也可由 .env 文件提供）
if not defined CPUSTACK_DATA_DIR set CPUSTACK_DATA_DIR=./data
if not defined CPUSTACK_MODEL_CACHE_DIR set CPUSTACK_MODEL_CACHE_DIR=./data/cache
if not defined CPUSTACK_DB_URL set CPUSTACK_DB_URL=sqlite+aiosqlite:///./data/cpustack.db

REM 1. 初始化数据库
echo [1/2] 初始化数据库...
py init_db.py
if errorlevel 1 (
    echo 数据库初始化失败
    pause
    exit /b 1
)

REM 2. 启动 Server（同时内嵌 Worker 单机模式）
echo [2/2] 启动 CPUSTACK Server (端口 8080)...
py -m cpustack.cli both --host 0.0.0.0 --port 8080
pause
