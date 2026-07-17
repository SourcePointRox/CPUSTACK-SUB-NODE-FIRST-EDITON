#!/bin/bash
echo "========================================"
echo "  CPUSTACK 主计算节点启动"
echo "========================================"
cd "$(dirname "$0")/backend"
python3 -m cpustack.cli both --host 0.0.0.0 --port 8081
