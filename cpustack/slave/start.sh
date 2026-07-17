#!/bin/bash
echo "========================================"
echo "  CPUSTACK 子计算节点启动"
echo "========================================"
cd "$(dirname "$0")/backend"
if [ -z "$1" ]; then
    echo "用法: ./start.sh <主节点IP> <主节点端口> [token]"
    echo "示例: ./start.sh 192.168.1.240 8081 cpustack-cluster-token"
    read -p "请输入主节点IP: " MASTER_IP
    read -p "请输入主节点端口(默认8081): " MASTER_PORT
    MASTER_PORT=${MASTER_PORT:-8081}
    python3 -m cpustack.cli worker --server-url http://$MASTER_IP:$MASTER_PORT --token cpustack-cluster-token
else
    MASTER_PORT=${2:-8081}
    TOKEN=${3:-cpustack-cluster-token}
    python3 -m cpustack.cli worker --server-url http://$1:$MASTER_PORT --token $TOKEN
fi
