@echo off
chcp 65001 >nul
echo ========================================
echo   CPUSTACK 子计算节点启动
echo ========================================
cd /d "%~dp0\backend"
if "%~1"=="" (
    echo 用法: start.bat ^<主节点IP^> ^<主节点端口^> [token]
    echo 示例: start.bat 192.168.1.240 8081 cpustack-cluster-token
    set /p MASTER_IP="请输入主节点IP: "
    set /p MASTER_PORT="请输入主节点端口(默认8081): "
    if "%MASTER_PORT%"=="" set MASTER_PORT=8081
    python -m cpustack.cli worker --server-url http://%MASTER_IP%:%MASTER_PORT% --token cpustack-cluster-token
) else (
    if "%~2"=="" (
        python -m cpustack.cli worker --server-url http://%~1:8081 --token cpustack-cluster-token
    ) else (
        if "%~3"=="" (
            python -m cpustack.cli worker --server-url http://%~1:%~2 --token cpustack-cluster-token
        ) else (
            python -m cpustack.cli worker --server-url http://%~1:%~2 --token %~3
        )
    )
)
pause
