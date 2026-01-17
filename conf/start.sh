#!/bin/bash
# 启动入口脚本

echo "=========================================="
echo "  Vaultwarden Backup Admin Starting..."
echo "=========================================="

# 检查是否配置了 DDNSTO Token
if [ -n "$DDNSTO_TOKEN" ] && [ -f /usr/local/bin/ddnsto ]; then
    echo "[DDNSTO] Token detected, enabling DDNSTO service..."
    # 使用 supervisorctl 启动 ddnsto
    export DDNSTO_ENABLED=1
else
    echo "[DDNSTO] No token configured, DDNSTO disabled."
    export DDNSTO_ENABLED=0
fi

# 启动 supervisord
exec /usr/bin/supervisord -c /etc/supervisord.conf
